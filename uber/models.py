from uber.common import *


def _get_defaults(func):
    spec = inspect.getfullargspec(func)
    return dict(zip(reversed(spec.args), reversed(spec.defaults)))
default_constructor = _get_defaults(declarative.declarative_base)['constructor']


SQLALchemyColumn = Column
def Column(*args, **kwargs):
    kwargs.setdefault('nullable', False)
    if args[0] is UnicodeText or isinstance(args[0], (UnicodeText, MultiChoice)):
        kwargs.setdefault('default', '')
    default = kwargs.get('default')
    if isinstance(default, (int, str)):
        kwargs.setdefault('server_default', str(default))
    return SQLALchemyColumn(*args, **kwargs)

sqlalchemy_relationship = relationship
def relationship(*args, **kwargs):
    kwargs.setdefault('load_on_pending', True)
    return sqlalchemy_relationship(*args, **kwargs)


class utcnow(FunctionElement):
    type = UTCDateTime()

@compiles(utcnow, 'postgresql')
def pg_utcnow(element, compiler, **kw):
    return "timezone('utc', current_timestamp)"

@compiles(utcnow, 'sqlite')
def sqlite_utcnow(element, compiler, **kw):
    return "(datetime('now', 'utc'))"



class Choice(TypeDecorator):
    impl = Integer

    def __init__(self, choices, *, allow_unspecified=False, **kwargs):
        self.choices = dict(choices)
        self.allow_unspecified = allow_unspecified
        TypeDecorator.__init__(self, **kwargs)

    def process_bind_param(self, value, dialect):
        if value is not None:
            try:
                assert self.allow_unspecified or int(value) in self.choices
            except:
                raise ValueError('{!r} not a valid option out of {}'.format(value, self.choices))
            else:
                return int(value)


class MultiChoice(TypeDecorator):
    impl = UnicodeText

    def __init__(self, choices, **kwargs):
        self.choices = choices
        TypeDecorator.__init__(self, **kwargs)

    def process_bind_param(self, value, dialect):
        return value if isinstance(value, str) else ','.join(value)


@declarative_base
class MagModel:
    id = Column(UUID, primary_key=True, default=lambda: str(uuid4()))

    _unrestricted = ()
    _propertized = ()

    def __init__(self, *args, **kwargs):
        if '_model' in kwargs:
            assert kwargs.pop('_model') == self.__class__.__name__
        default_constructor(self, *args, **kwargs)
        for attr, col in self.__table__.columns.items():
            if col.default:
                self.__dict__.setdefault(attr, col.default.execute())

    def presave_adjustments(self):
        pass

    def on_delete(self):
        pass

    @property
    def session(self):
        return Session.session_factory.object_session(self)

    @classmethod
    def get_field(cls, name):
        return cls.__table__.columns[name]

    def __eq__(self, m):
        return self.id is not None and isinstance(m, MagModel) and self.id == m.id

    def __ne__(self, m):        # Python is stupid for making me do this
        return not (self == m)

    def __hash__(self):
        return hash(self.id)

    @property
    def is_new(self):
        return not instance_state(self).persistent

    @property
    def created(self):
        return self.get_tracking_by_instance(self, action=CREATED, last_only=True)

    @property
    def last_updated(self):
        return self.get_tracking_by_instance(self, action=UPDATED, last_only=True)

    @property
    def db_id(self):
        return None if self.is_new else self.id

    def orig_value_of(self, name):
        hist = get_history(self, name)
        return (hist.deleted or hist.unchanged or [getattr(self, name)])[0]

    @suffix_property
    def _ints(self, name, val):
        choices = dict(self.get_field(name).type.choices)
        return [int(i) for i in str(val).split(',') if int(i) in choices] if val else []

    @suffix_property
    def _label(self, name, val):
        return '' if val is None else self.get_field(name).type.choices[int(val)]

    @suffix_property
    def _local(self, name, val):
        return val.astimezone(EVENT_TIMEZONE)

    @suffix_property
    def _labels(self, name, val):
        ints = getattr(self, name + '_ints')
        labels = dict(self.get_field(name).type.choices)
        return sorted(labels[i] for i in ints)

    def __getattr__(self, name):
        suffixed = suffix_property.check(self, name)
        if suffixed is not None:
            return suffixed

        try:
            [multi] = [col for col in self.__table__.columns if isinstance(col.type, MultiChoice)]
            choice = getattr(config, name)
            assert choice in [val for val, desc in multi.type.choices]
        except:
            pass
        else:
            return choice in getattr(self, multi.name + '_ints')

        if name.startswith('is_'):
            return self.__class__.__name__.lower() == name[3:]

        raise AttributeError(self.__class__.__name__ + '.' + name)

    def get_tracking_by_instance(self, instance, action, last_only=True):
        query = self.session.query(Tracking).filter_by(fk_id=instance.id, action=action).order_by(Tracking.when.desc())
        return query.first() if last_only else query.all()

    # NOTE: if we used from_dict() to implement this it might end up being simpler
    def apply(self, params, *, bools=(), checkgroups=(), restricted=True, ignore_csrf=True):
        for column in self.__table__.columns:
            if (not restricted or column.name in self._unrestricted) and column.name in params and column.name != 'id':
                if isinstance(params[column.name], list):
                    value = ','.join(map(str, params[column.name]))
                elif isinstance(params[column.name], bool):
                    value = params[column.name]
                else:
                    value = str(params[column.name]).strip()

                try:
                    if isinstance(column.type, Float):
                        value = float(value)
                    elif isinstance(column.type, (Choice, Integer)):
                        value = int(float(value))
                    elif isinstance(column.type, UTCDateTime):
                        value = EVENT_TIMEZONE.localize(datetime.strptime(value, TIMESTAMP_FORMAT))
                    elif isinstance(column.type, Date):
                        value = datetime.strptime(value, DATE_FORMAT).date()
                except:
                    pass

                setattr(self, column.name, value)

        if cherrypy.request.method.upper() == 'POST':
            for column in self.__table__.columns:
                if column.name in bools:
                    setattr(self, column.name, column.name in params and bool(int(params[column.name])))
                elif column.name in checkgroups and column.name not in params:
                    setattr(self, column.name, '')

            if not ignore_csrf:
                check_csrf(params.get('csrf_token'))


class TakesPaymentMixin(object):
    @property
    def payment_deadline(self):
        return min(UBER_TAKEDOWN - timedelta(days = 2),
                   datetime.combine((self.registered + timedelta(days = 14)).date(), time(23, 59)))

def _night(name):
    day = getattr(config, name.upper())
    def lookup(self):
        return day if day in self.nights_ints else ''
    lookup.__name__ = name
    lookup = property(lookup)
    def setter(self, val):
        if val:
            self.nights = '{},{}'.format(self.nights, day).strip(',')
        else:
            self.nights = ','.join([str(night) for night in self.nights_ints if night != day])
    setter.__name__ = name
    return lookup.setter(setter)

class NightsMixin(object):
    @property
    def nights_display(self):
        ordered = sorted(self.nights_ints, key=NIGHT_DISPLAY_ORDER.index)
        return ' / '.join(dict(NIGHT_OPTS)[val] for val in ordered)

    @property
    def setup_teardown(self):
        return any(night for night in self.nights_ints if night not in CORE_NIGHTS)

    locals().update({mutate(name): _night(mutate(name)) for name in NIGHT_NAMES for mutate in [str.upper, str.lower]})


class Event(MagModel):
    location    = Column(Choice(EVENT_LOCATION_OPTS))
    start_time  = Column(UTCDateTime)
    duration    = Column(Integer)   # half-hour increments
    name        = Column(UnicodeText, nullable=False)
    description = Column(UnicodeText)

    @property
    def half_hours(self):
        half_hours = set()
        for i in range(self.duration):
            half_hours.add(self.start_time + timedelta(minutes = 30 * i))
        return half_hours

    @property
    def minutes(self):
        return (self.duration or 0) * 30

    @property
    def start_slot(self):
        if self.start_time:
            return int((self.start_time_local - EPOCH).total_seconds() / (60 * 30))


class Group(MagModel, TakesPaymentMixin):
    name          = Column(UnicodeText)
    tables        = Column(Float, default=0)
    address       = Column(UnicodeText)
    website       = Column(UnicodeText)
    wares         = Column(UnicodeText)
    description   = Column(UnicodeText)
    special_needs = Column(UnicodeText)
    amount_paid   = Column(Integer, default=0)
    cost          = Column(Integer, default=0)
    auto_recalc   = Column(Boolean, default=True)
    status        = Column(Choice(DEALER_STATUS_OPTS), default=UNAPPROVED)
    table_extras  = Column(MultiChoice(TABLE_EXTRA_OPTS))
    can_add       = Column(Boolean, default=False)
    admin_notes   = Column(UnicodeText)
    registered    = Column(UTCDateTime, server_default=utcnow())
    approved      = Column(UTCDateTime, nullable=True)
    leader_id     = Column(UUID, ForeignKey('attendee.id', use_alter=True, name='fk_leader'), nullable=True)
    leader        = relationship('Attendee', foreign_keys=leader_id, post_update=True)

    _repr_attr_names = ['name']
    _unrestricted = {'name', 'tables', 'address', 'website', 'wares', 'description', 'special_needs', 'table_extras'}

    def presave_adjustments(self):
        assigned = [a for a in self.attendees if not a.is_unassigned]
        if len(assigned) == 1:
            [self.leader] = assigned
        if self.auto_recalc:
            self.cost = self.default_cost
        if self.status == APPROVED and not self.approved:
            self.approved = datetime.now(UTC)

        if self.leader and self.is_dealer:
            self.leader.ribbon = DEALER_RIBBON

    @property
    def is_new(self):
        return not instance_state(self).persistent

    @property
    def sorted_attendees(self):
        self.attendees.sort(key=lambda a: (a.is_unassigned, a.id != self.leader_id, a.full_name))
        return self.attendees

    @property
    def floating(self):
        return [a for a in self.attendees if a.is_unassigned and a.paid == PAID_BY_GROUP]

    @property
    def new_ribbon(self):
        ribbons = {a.ribbon for a in self.attendees}
        for ribbon in [BAND_RIBBON]:
            if ribbon in ribbons:
                return ribbon
        else:
            return DEALER_ASST_RIBBON if self.is_dealer else NO_RIBBON

    @property
    def ribbon_and_or_badge(self):
        badge_being_claimed = self.floating[0]
        if badge_being_claimed.ribbon != NO_RIBBON and badge_being_claimed.badge_type != ATTENDEE_BADGE:
            return badge_being_claimed.badge_type_label + " / " + self.ribbon_label
        elif badge_being_claimed.ribbon != NO_RIBBON:
            return badge_being_claimed.ribbon_label
        else:
            return badge_being_claimed.badge_type_label

    @property
    def is_dealer(self):
        return bool(self.tables and (not self.registered or self.amount_paid or self.cost))

    @property
    def is_unpaid(self):
        return self.cost > 0 and self.amount_paid == 0

    @property
    def email(self):
        if self.leader and self.leader.email:
            return self.leader.email
        elif self.leader_id:  # unattached groups
            [leader] = [a for a in self.attendees if a.id == self.leader_id]
            return leader.email
        else:
            emails = [a.email for a in self.attendees if a.email]
            if len(emails) == 1:
                return emails[0]

    @property
    def badges_purchased(self):
        return len([a for a in self.attendees if a.paid == PAID_BY_GROUP])

    @property
    def badges(self):
        return len(self.attendees)

    @property
    def unregistered_badges(self):
        return len([a for a in self.attendees if a.is_unassigned])

    @property
    def table_cost(self):
        total = 0

        total += TABLE_PRICES.get(self.tables, 999)

        for extra, amount in TABLE_EXTRA_PRICES.items():
            if extra in self.table_extras_ints:
                total += amount
        return total

    @property
    def new_badge_cost(self):
        return DEALER_BADGE_PRICE if self.tables else state.get_group_price(localized_now())

    @property
    def badge_cost(self):
        total = 0
        for attendee in self.attendees:
            if attendee.paid == PAID_BY_GROUP:
                total += state.get_group_price(attendee.registered)
        return total

    @property
    def default_cost(self):
        return self.table_cost + self.badge_cost + self.amount_extra

    @property
    def amount_extra(self):
        if self.is_new:
            return sum(a.amount_unpaid for a in self.attendees if a.paid == PAID_BY_GROUP)
        else:
            return 0

    @property
    def amount_unpaid(self):
        if self.registered:
            return 0 if (self.cost - self.amount_paid) < 0 else (self.cost - self.amount_paid)
        else:
            return self.default_cost

    @property
    def dealer_max_badges(self):
        return math.ceil(self.tables) + 1

    @property
    def dealer_badges_remaining(self):
        return self.dealer_max_badges - self.badges

    @property
    def min_badges_addable(self):
        if self.is_dealer and self.badges >= self.dealer_max_badges:
            return 0
        elif self.is_dealer or self.can_add:
            return 1
        else:
            return 5

class AgeGroup(MagModel):
    desc          = Column(UnicodeText)
    min_age       = Column(Integer)
    max_age       = Column(Integer)
    discount      = Column(Integer)
    can_register  = Column(Boolean, default=True)
    can_volunteer = Column(Boolean, default=True)
    consent_form  = Column(Boolean, default=False)

class Attendee(MagModel, TakesPaymentMixin):
    group_id = Column(UUID, ForeignKey('group.id', ondelete='SET NULL'), nullable=True)
    group = relationship(Group, backref='attendees', foreign_keys=group_id)

    status        = Column(Choice(BADGE_STATUS_OPTS), default=NEW_STATUS)
    placeholder   = Column(Boolean, default=False)
    first_name    = Column(UnicodeText)
    last_name     = Column(UnicodeText)
    email         = Column(UnicodeText)
    age_group_id  = Column(UUID, ForeignKey('age_group.id', ondelete='SET NULL'), nullable=True)
    age_group     = relationship(AgeGroup, backref='attendees', foreign_keys=age_group_id)
    birthdate     = Column(Date, nullable=True, default=None)

    international = Column(Boolean, default=False)
    zip_code      = Column(UnicodeText)
    address1      = Column(UnicodeText)
    address2      = Column(UnicodeText)
    city          = Column(UnicodeText)
    region        = Column(UnicodeText)
    country       = Column(UnicodeText)
    no_cellphone  = Column(Boolean, default=False)
    ec_name       = Column(UnicodeText)
    ec_phone      = Column(UnicodeText)
    cellphone     = Column(UnicodeText)

    interests   = Column(MultiChoice(INTEREST_OPTS))
    found_how   = Column(UnicodeText)
    comments    = Column(UnicodeText)
    for_review  = Column(UnicodeText)
    admin_notes = Column(UnicodeText)

    badge_num  = Column(Integer, default=0, nullable=True)
    badge_type = Column(Choice(BADGE_OPTS), default=ATTENDEE_BADGE)
    ribbon     = Column(Choice(RIBBON_OPTS), default=NO_RIBBON)

    affiliate    = Column(UnicodeText)
    shirt        = Column(Choice(SHIRT_OPTS), default=NO_SHIRT)
    can_spam     = Column(Boolean, default=False)
    regdesk_info = Column(UnicodeText)
    extra_merch  = Column(UnicodeText)
    got_merch    = Column(Boolean, default=False)

    reg_station   = Column(Integer, nullable=True)
    registered = Column(UTCDateTime, server_default=utcnow())
    checked_in = Column(UTCDateTime, nullable=True)

    paid             = Column(Choice(PAYMENT_OPTS), default=NOT_PAID)
    overridden_price = Column(Integer, nullable=True)
    amount_paid      = Column(Integer, default=0)
    amount_extra     = Column(Choice(DONATION_TIER_OPTS, allow_unspecified=True), default=0)
    amount_refunded  = Column(Integer, default=0)
    payment_method   = Column(Choice(PAYMENT_METHOD_OPTS), nullable=True)

    badge_printed_name = Column(UnicodeText)

    staffing         = Column(Boolean, default=False)
    fire_safety_cert = Column(UnicodeText)
    requested_depts  = Column(MultiChoice(JOB_INTEREST_OPTS))
    assigned_depts   = Column(MultiChoice(JOB_LOCATION_OPTS))
    trusted          = Column(Boolean, default=False)
    nonshift_hours   = Column(Integer, default=0)
    past_years       = Column(UnicodeText)

    no_shirt          = relationship('NoShirt', backref=backref('attendee', load_on_pending=True), uselist=False, cascade='all,delete-orphan')
    admin_account     = relationship('AdminAccount', backref=backref('attendee', load_on_pending=True), uselist=False, cascade='all,delete-orphan')
    hotel_requests    = relationship('HotelRequests', backref=backref('attendee', load_on_pending=True), uselist=False, cascade='all,delete-orphan')
    room_assignments  = relationship('RoomAssignment', backref=backref('attendee', load_on_pending=True), uselist=False, cascade='all,delete-orphan')
    food_restrictions = relationship('FoodRestrictions', backref=backref('attendee', load_on_pending=True), uselist=False, cascade='all,delete-orphan')

    _repr_attr_names = ['full_name']
    _unrestricted = {'first_name', 'last_name', 'international', 'zip_code', 'address1', 'address2', 'city', 'region', 'country', 'ec_name',
                     'ec_phone', 'cellphone', 'email', 'age_group', 'birthdate', 'interests', 'found_how', 'comments', 'badge_type',
                     'affiliate', 'shirt', 'can_spam', 'no_cellphone', 'badge_printed_name', 'staffing', 'fire_safety_cert', 'requested_depts',
                     'amount_extra', 'payment_method'}

    def on_delete(self):
        #_assert_badge_lock()
        if self.has_personalized_badge and SHIFT_CUSTOM_BADGES:
            self.session.shift_badges(self.badge_type, self.badge_num, down=True)

    def presave_adjustments(self):
        self._staffing_adjustments()
        self._badge_adjustments()
        self._status_adjustments()
        self._misc_adjustments()

    def _misc_adjustments(self):
        if not self.amount_extra:
            self.affiliate = ''

        if self.birthdate == '':
            self.birthdate = None

        if not self.shirt_eligible:
            self.shirt = NO_SHIRT

        if self.paid != REFUNDED:
            self.amount_refunded = 0

        if AT_THE_CON and self.badge_num and self.is_new:
            self.checked_in = datetime.now(UTC)
            
        if COLLECT_EXACT_BIRTHDATE:
            self.age_group = self.session.age_group_from_birthdate(self.birthdate)

        for attr in ['first_name', 'last_name']:
            value = getattr(self, attr)
            if value.isupper() or value.islower():
                setattr(self, attr, value.title())

    def _status_adjustments(self):
        old_status = self.orig_value_of('status')
        old_amount_paid = self.orig_value_of('amount_paid')
        if old_status == self.status and old_amount_paid != self.amount_paid:
            if self.paid == NOT_PAID or self.placeholder:
                self.status = NEW_STATUS
            elif self.paid == HAS_PAID or self.paid == NEED_NOT_PAY:
                self.status = COMPLETED_STATUS

    def _badge_adjustments(self):
        #_assert_badge_lock()

        if self.badge_type in [PSEUDO_GROUP_BADGE, PSEUDO_DEALER_BADGE]:
            self.badge_type = ATTENDEE_BADGE
            if self.is_dealer:
                self.ribbon = DEALER_RIBBON

        if self.amount_extra >= SUPPORTER_LEVEL and not self.amount_unpaid and self.badge_type == ATTENDEE_BADGE:
            self.badge_type = SUPPORTER_BADGE

        if PRE_CON:
            if self.paid == NOT_PAID or not self.has_personalized_badge or self.is_unassigned:
                self.badge_num = 0
            elif self.has_personalized_badge and not self.badge_num:
                if self.paid != NOT_PAID:
                    self.badge_num = self.session.next_badge_num(self.badge_type, old_badge_num=0)

    def _staffing_adjustments(self):
        if self.ribbon == DEPT_HEAD_RIBBON:
            self.staffing = self.trusted = True
            self.badge_type = STAFF_BADGE
            if self.paid == NOT_PAID:
                self.paid = NEED_NOT_PAY

        if not self.is_new:
            old_ribbon = self.orig_value_of('ribbon')
            old_staffing = self.orig_value_of('staffing')
            if self.staffing and not old_staffing or self.ribbon == VOLUNTEER_RIBBON and old_ribbon != VOLUNTEER_RIBBON:
                self.staffing = True
                if self.ribbon == NO_RIBBON:
                    self.ribbon = VOLUNTEER_RIBBON
            elif COLLECT_INTERESTS and old_staffing and not self.staffing or self.ribbon != VOLUNTEER_RIBBON and old_ribbon == VOLUNTEER_RIBBON:
                self.unset_volunteering()

        if self.badge_type == STAFF_BADGE and self.ribbon == VOLUNTEER_RIBBON:
            self.ribbon = NO_RIBBON

        if self.badge_type == STAFF_BADGE:
            self.staffing = True

    def unset_volunteering(self):
        self.staffing = self.trusted = False
        self.requested_depts = self.assigned_depts = ''
        if self.ribbon == VOLUNTEER_RIBBON:
            self.ribbon = NO_RIBBON
        if self.badge_type == STAFF_BADGE:
            if SHIFT_CUSTOM_BADGES: self.session.shift_badges(STAFF_BADGE, self.badge_num, down=True)
            self.badge_type = ATTENDEE_BADGE
        del self.shifts[:]

    @property
    def ribbon_and_or_badge(self):
        if self.ribbon != NO_RIBBON and self.badge_type != ATTENDEE_BADGE:
            return self.badge_type_label + " / " + self.ribbon_label
        elif self.ribbon != NO_RIBBON:
            return self.ribbon_label
        else:
            return self.badge_type_label

    @property
    def badge_cost(self):
        registered = self.registered_local if self.registered else localized_now()
        if self.paid in [PAID_BY_GROUP, NEED_NOT_PAY]:
            return 0
        elif self.overridden_price is not None:
            return self.overridden_price
        elif self.badge_type == ONE_DAY_BADGE:
            return state.get_oneday_price(registered)
        else:
            return state.get_attendee_price(registered)

    @property
    def total_cost(self):
        if self.amount_extra in FREE_KICKIN_LEVELS:
            return self.badge_cost
        else:
            return self.badge_cost + self.amount_extra

    @property
    def amount_unpaid(self):
        return max(0, self.total_cost - self.amount_paid)

    @property
    def is_unpaid(self):
        return self.paid == NOT_PAID

    @property
    def is_unassigned(self):
        return not self.first_name

    @property
    def is_dealer(self):
        return self.ribbon == DEALER_RIBBON or self.badge_type == PSEUDO_DEALER_BADGE

    @property
    def is_dept_head(self):
        return self.ribbon == DEPT_HEAD_RIBBON

    @property
    def is_group_leader(self):
        return self.group and self.id == self.group.leader_id

    @property
    def shirt_size_marked(self):
        return self.shirt not in [NO_SHIRT, SIZE_UNKNOWN]

    @property
    def is_group_leader(self):
        return self.group and self.id == self.group.leader_id

    @property
    def unassigned_name(self):
        if self.group_id and self.is_unassigned:
            return '[Unassigned {self.badge}]'.format(self=self)

    @hybrid_property
    def full_name(self):
        return self.unassigned_name or '{self.first_name} {self.last_name}'.format(self=self)

    @full_name.expression
    def full_name(cls):
        return case([
            (or_(cls.first_name == None, cls.first_name == ''), 'zzz')
        ], else_ = func.lower(cls.first_name + ' ' + cls.last_name))

    @hybrid_property
    def last_first(self):
        return self.unassigned_name or '{self.last_name}, {self.first_name}'.format(self=self)

    @last_first.expression
    def last_first(cls):
        return case([
            (or_(cls.first_name == None, cls.first_name == ''), 'zzz')
        ], else_ = func.lower(cls.last_name + ', ' + cls.first_name))
        
    @property
    def can_volunteer(self):
        if self.age_group: return self.age_group.can_volunteer
        with Session() as session:
            return session.age_group_from_birthdate(self.birthdate).can_volunteer
            
    @property
    def can_register(self):
        if self.age_group: return self.age_group.can_register
        with Session() as session:
            return session.age_group_from_birthdate(self.birthdate).can_register
            
    @property
    def age_discount(self):
        if self.age_group: return self.age_group.discount
        with Session() as session:
            return session.age_group_from_birthdate(self.birthdate).discount
            
    @property
    def consent_form(self):
        if self.age_group: return self.age_group.consent_form
        with Session() as session:
            return session.age_group_from_birthdate(self.birthdate).consent_form

    @property
    def age_group_desc(self):
        if self.age_group: return self.age_group.desc
        with Session() as session:
            return session.age_group_from_birthdate(self.birthdate).desc

    @property
    def banned(self):
        return self.full_name in BANNED_ATTENDEES

    @property
    def badge(self):
        if self.paid == NOT_PAID:
            badge = 'Unpaid ' + self.badge_type_label
        elif self.badge_num:
            badge = '{} #{}'.format(self.badge_type_label, self.badge_num)
        else:
            badge = self.badge_type_label

        if self.ribbon != NO_RIBBON:
            badge += ' ({})'.format(self.ribbon_label)

        return badge

    @property
    def is_transferable(self):
        return not self.is_new and not self.trusted and not self.checked_in \
           and self.paid in [HAS_PAID, PAID_BY_GROUP] \
           and self.badge_type in TRANSFERABLE_BADGE_TYPES

    @property
    def gets_free_shirt(self):
        return self.is_dept_head \
            or self.badge_type == STAFF_BADGE \
            or self.staffing and (self.assigned_depts and not self.takes_shifts or self.weighted_hours >= 6)

    @property
    def gets_paid_shirt(self):
        return self.amount_extra >= SHIRT_LEVEL or self.badge_type == SUPPORTER_BADGE

    @property
    def gets_shirt(self):
        return self.gets_paid_shirt or self.gets_free_shirt

    @property
    def shirt_eligible(self):
        return self.gets_shirt or self.staffing

    @property
    def has_personalized_badge(self):
        return self.badge_type in PREASSIGNED_BADGE_TYPES

    @property
    def donation_swag(self):
        extra = SUPPORTER_LEVEL if not self.amount_extra and self.badge_type == SUPPORTER_BADGE else self.amount_extra
        return [desc for amount,desc in sorted(DONATION_TIERS.items()) if amount and extra >= amount]

    @property
    def merch(self):
        merch = self.donation_swag
        if self.gets_shirt and DONATION_TIERS[SHIRT_LEVEL] not in merch:
            merch.append(DONATION_TIERS[SHIRT_LEVEL])
        elif self.gets_free_shirt:
            shirt = '2nd ' + DONATION_TIERS[SHIRT_LEVEL]
            if self.takes_shifts and self.worked_hours < 6:
                shirt += ' (tell them they will be reported if they take their shirt and then do not work their shifts)'
            merch.append(shirt)
        if self.extra_merch:
            merch.append(self.extra_merch)
        return comma_and(merch)

    @property
    def accoutrements(self):
        stuff = [] if self.ribbon == NO_RIBBON else ['a ' + self.ribbon_label + ' ribbon']
        stuff.append('a {} wristband'.format(WRISTBAND_COLORS[self.age_group]))
        if self.regdesk_info:
            stuff.append(self.regdesk_info)
        return comma_and(stuff)

    @property
    def is_single_dept_head(self):
        return self.is_dept_head and len(self.assigned_depts_ints) == 1

    @property
    def multiply_assigned(self):
        return len(self.assigned_depts_ints) > 1

    @property
    def takes_shifts(self):
        return bool(self.staffing and set(self.assigned_depts_ints) - SHIFTLESS_DEPTS)

    @property
    def hotel_shifts_required(self):
        return bool(SHIFTS_CREATED and self.hotel_nights and self.ribbon != DEPT_HEAD_RIBBON and self.takes_shifts)

    @property
    def approved_for_setup(self):
        hr = self.hotel_requests
        return bool(hr and hr.approved and set(hr.nights_ints).intersection(SETUP_NIGHTS))

    @property
    def approved_for_teardown(self):
        hr = self.hotel_requests
        return bool(hr and hr.approved and set(hr.nights_ints).intersection(TEARDOWN_NIGHTS))

    @property
    def hours(self):
        all_hours = set()
        for shift in self.shifts:
            all_hours.update(shift.job.hours)
        return all_hours

    @property
    def hour_map(self):
        all_hours = {}
        for shift in self.shifts:
            for hour in shift.job.hours:
                all_hours[hour] = shift.job
        return all_hours

    @cached_property
    def possible(self):
        assert self.session, '.possible property may only be accessed for jobs attached to a session'
        if not self.assigned_depts and not AT_THE_CON:
            return []
        else:
            return [job for job in self.session.query(Job)
                                       .filter(*[] if AT_THE_CON else [Job.location.in_(self.assigned_depts_ints)])
                                       .options(joinedload(Job.shifts))
                                       .order_by(Job.start_time).all()
                        if job.slots > len(job.shifts)
                           and job.no_overlap(self)
                           and (job.type != SETUP or self.approved_for_setup)
                           and (job.type != TEARDOWN or self.approved_for_teardown)
                           and (not job.restricted or self.trusted)]

    @property
    def possible_opts(self):
        return [(job.id, '(%s) [%s] %s' % (hour_day_format(job.start_time), job.location_label, job.name))
                for job in self.possible if localized_now() < job.start_time]

    @property
    def possible_and_current(self):
        jobs = [s.job for s in self.shifts]
        for job in jobs:
            job.taken = True
        jobs.extend(self.possible)
        return sorted(jobs, key=lambda j: j.start_time)

    @property
    def worked_shifts(self):
        return [shift for shift in self.shifts if shift.worked == SHIFT_WORKED]

    @property
    def weighted_hours(self):
        wh = sum((shift.job.weighted_hours for shift in self.shifts), 0.0)
        return wh + self.nonshift_hours

    @property
    def worked_hours(self):
        wh = sum((shift.job.real_duration * shift.job.weight for shift in self.worked_shifts), 0.0)
        return wh + self.nonshift_hours

    def requested(self, department):
        return department in self.requested_depts_ints

    def assigned_to(self, department):
        return int(department or 0) in self.assigned_depts_ints

    def has_shifts_in(self, department):
        return any(shift.job.location == department for shift in self.shifts)

    @property
    def shift_prereqs_complete(self):
        return not self.placeholder and self.food_restrictions and self.shirt_size_marked \
            and (self.badge_type != STAFF_BADGE or self.hotel_requests or not state.BEFORE_ROOM_DEADLINE)

    @property
    def past_years_json(self):
        return json.loads(self.past_years or '[]')

    @property
    def hotel_eligible(self):
        return ROOM_DEADLINE and self.badge_type == STAFF_BADGE

    @property
    def hotel_nights(self):
        try:
            return self.hotel_requests.nights
        except:
            return []

    @cached_property
    def hotel_status(self):
        hr = self.hotel_requests
        if not hr:
            return 'Has not filled out volunteer checklist'
        elif not hr.nights:
            return 'Declined hotel space'
        elif hr.setup_teardown:
            return 'Hotel nights: {} ({})'.format(hr.nights_display, 'approved' if hr.approved else 'not yet approved')
        else:
            return 'Hotel nights: ' + hr.nights_display


class AdminAccount(MagModel):
    attendee_id = Column(UUID, ForeignKey('attendee.id'), unique=True)
    hashed      = Column(UnicodeText)
    access      = Column(MultiChoice(ACCESS_OPTS))

    password_reset = relationship('PasswordReset', backref='admin_account', uselist=False, cascade='all,delete-orphan')

    def __repr__(self):
        return '<{}>'.format(self.attendee.full_name)

    @staticmethod
    def is_nick():
        return AdminAccount.admin_name() in JERKS

    @staticmethod
    def admin_name():
        try:
            with Session() as session:
                return session.admin_attendee().full_name
        except:
            return None

    @staticmethod
    def access_set(id=None):
        try:
            with Session() as session:
                id = id or cherrypy.session['account_id']
                return set(session.admin_account(id).access_ints)
        except:
            return set()

class PasswordReset(MagModel):
    account_id = Column(UUID, ForeignKey('admin_account.id'), unique=True)
    generated  = Column(UTCDateTime, server_default=utcnow())
    hashed     = Column(UnicodeText)

    @property
    def is_expired(self):
        return self.generated < datetime.now(UTC) - timedelta(days=7)

class HotelRequests(MagModel, NightsMixin):
    attendee_id        = Column(UUID, ForeignKey('attendee.id'), unique=True)
    nights             = Column(MultiChoice(NIGHT_OPTS))
    wanted_roommates   = Column(UnicodeText)
    unwanted_roommates = Column(UnicodeText)
    special_needs      = Column(UnicodeText)
    approved           = Column(Boolean, default=False)

    _unrestricted = ['attendee_id', 'nights', 'wanted_roommates', 'unwanted_roommates', 'special_needs']

    def decline(self):
        self.nights = ','.join(night for night in self.nights.split(',') if int(night) in CORE_NIGHTS)

    def __repr__(self):
        return '<{self.attendee.full_name} Hotel Requests>'.format(self=self)

class FoodRestrictions(MagModel):
    attendee_id   = Column(UUID, ForeignKey('attendee.id'), unique=True)
    standard      = Column(MultiChoice(FOOD_RESTRICTION_OPTS))
    sandwich_pref = Column(Choice(SANDWICH_OPTS))
    no_cheese     = Column(Boolean, default=False)
    freeform      = Column(UnicodeText)

    def __getattr__(self, name):
        restriction = globals().get(name.upper())
        if restriction not in FOOD_RESTRICTIONS:
            return MagModel.__getattr__(self, name)
        elif restriction == VEGETARIAN and VEGAN in self.standard_ints:
            return False
        elif restriction == PORK and {VEGETARIAN, VEGAN}.intersection(self.standard_ints):
            return True
        else:
            return restriction in self.standard_ints

class AssignedPanelist(MagModel):
    attendee_id = Column(UUID, ForeignKey('attendee.id', ondelete='cascade'))
    attendee    = relationship(Attendee, backref=backref('assigned_panelists', cascade='all,delete-orphan'))
    event_id    = Column(UUID, ForeignKey('event.id', ondelete='cascade'))
    event       = relationship(Event, backref=backref('assigned_panelists', cascade='all,delete-orphan'))

    def __repr__(self):
        return '<{self.attendee.full_name} panelisting {self.event.name}>'.format(self=self)

class SeasonPassTicket(MagModel):
    fk_id    = Column(UUID)
    slug     = Column(UnicodeText)

    @property
    def fk(self):
        return self.session.season_pass(self.fk_id)

class Room(MagModel, NightsMixin):
    department = Column(Choice(JOB_LOCATION_OPTS))
    notes      = Column(UnicodeText)
    nights     = Column(MultiChoice(NIGHT_OPTS))
    created    = Column(UTCDateTime, server_default=utcnow())

class RoomAssignment(MagModel):
    room_id     = Column(UUID, ForeignKey('room.id'))
    room        = relationship(Room, backref='room_assignments')
    attendee_id = Column(UUID, ForeignKey('attendee.id'), unique=True)

class NoShirt(MagModel):
    attendee_id = Column(UUID, ForeignKey('attendee.id'), unique=True)

class MerchPickup(MagModel):
    picked_up_by_id  = Column(UUID, ForeignKey('attendee.id'))
    picked_up_for_id = Column(UUID, ForeignKey('attendee.id'), unique=True)
    picked_up_by     = relationship(Attendee, primaryjoin='MerchPickup.picked_up_by_id == Attendee.id')
    picked_up_for    = relationship(Attendee, primaryjoin='MerchPickup.picked_up_for_id == Attendee.id')

class DeptChecklistItem(MagModel):
    attendee_id = Column(UUID, ForeignKey('attendee.id'))
    attendee    = relationship(Attendee, backref=backref('dept_checklist_items', cascade='all,delete-orphan'))
    slug        = Column(UnicodeText)
    comments    = Column(UnicodeText, default='')

    __table_args__ = (
        UniqueConstraint('attendee_id', 'slug', name='_dept_checklist_item_uniq'),
    )


class Job(MagModel):
    type        = Column(Choice(JOB_TYPE_OPTS), default=REGULAR)
    name        = Column(UnicodeText)
    description = Column(UnicodeText)
    location    = Column(Choice(JOB_LOCATION_OPTS))
    start_time  = Column(UTCDateTime)
    duration    = Column(Integer)
    weight      = Column(Float, default=1)
    slots       = Column(Integer)
    restricted  = Column(Boolean, default=False)
    extra15     = Column(Boolean, default=False)

    _repr_attr_names = ['name']

    @property
    def hours(self):
        hours = set()
        for i in range(self.duration):
            hours.add(self.start_time + timedelta(hours=i))
        return hours

    @property
    def end_time(self):
        return self.start_time + timedelta(hours=self.duration)

    def no_overlap(self, attendee):
        before = self.start_time - timedelta(hours=1)
        after  = self.start_time + timedelta(hours=self.duration)
        return (not self.hours.intersection(attendee.hours)
            and (before not in attendee.hour_map
                or not attendee.hour_map[before].extra15
                or self.location == attendee.hour_map[before].location)
            and (after not in attendee.hour_map
                or not self.extra15
                or self.location == attendee.hour_map[after].location))

    @property
    def slots_taken(self):
        return len(self.shifts)

    @property
    def slots_untaken(self):
        return max(0, self.slots - self.slots_taken)

    @property
    def is_setup(self):
        return self.start_time < EPOCH

    @property
    def is_teardown(self):
        return self.start_time >= ESCHATON

    @property
    def real_duration(self):
        return self.duration + (0.25 if self.extra15 else 0)

    @property
    def weighted_hours(self):
        return self.weight * self.real_duration

    @property
    def total_hours(self):
        return self.weighted_hours * self.slots

    @cached_property
    def all_staffers(self):
        return self.session.query(Attendee).order_by(Attendee.last_first).all()

    @cached_property
    def available_staffers(self):
        return [s for s in self.all_staffers
                if self.location in s.assigned_depts_ints
                   and (s.trusted or not self.restricted)
                   and self.no_overlap(s)]

class Shift(MagModel):
    job_id      = Column(UUID, ForeignKey('job.id', ondelete='cascade'))
    job         = relationship(Job, backref='shifts')
    attendee_id = Column(UUID, ForeignKey('attendee.id', ondelete='cascade'))
    attendee    = relationship(Attendee, backref='shifts')
    worked      = Column(Choice(WORKED_STATUS_OPTS), default=SHIFT_UNMARKED)
    rating      = Column(Choice(RATING_OPTS), default=UNRATED)
    comment     = Column(UnicodeText)

    @property
    def name(self):
        return "{self.attendee.full_name}'s {self.job.name!r} shift".format(self=self)

    @staticmethod
    def dump(shifts):
        return {shift.id: shift.to_dict() for shift in shifts}



class MPointsForCash(MagModel):
    attendee_id = Column(UUID, ForeignKey('attendee.id'))
    attendee    = relationship(Attendee, backref=backref('mpoints_for_cash', cascade='all,delete-orphan'))
    amount      = Column(Integer)
    when        = Column(UTCDateTime, default=lambda: datetime.now(UTC))

class OldMPointExchange(MagModel):
    attendee_id = Column(UUID, ForeignKey('attendee.id'))
    attendee    = relationship(Attendee, backref=backref('old_mpoint_exchanges', cascade='all,delete-orphan'))
    amount      = Column(Integer)
    when        = Column(UTCDateTime, default=lambda: datetime.now(UTC))

class Sale(MagModel):
    attendee_id    = Column(UUID, ForeignKey('attendee.id', ondelete='set null'), nullable=True)
    attendee       = relationship(Attendee, backref=backref('sales', cascade='all'))
    what           = Column(UnicodeText)
    cash           = Column(Integer, default=0)
    mpoints        = Column(Integer, default=0)
    when           = Column(UTCDateTime, default=lambda: datetime.now(UTC))
    reg_station    = Column(Integer, nullable=True)
    payment_method = Column(Choice(SALE_OPTS), default=MERCH)

class ArbitraryCharge(MagModel):
    amount      = Column(Integer)
    what        = Column(UnicodeText)
    when        = Column(UTCDateTime, default=lambda: datetime.now(UTC))
    reg_station = Column(Integer, nullable=True)

    _repr_attr_names = ['what']



class Game(MagModel):
    code        = Column(UnicodeText)
    name        = Column(UnicodeText)
    attendee_id = Column(UUID, ForeignKey('attendee.id'))
    attendee    = relationship(Attendee, backref=backref('games', cascade='all,delete-orphan'))
    returned    = Column(Boolean, default=False)

    @property
    def checked_out(self):
        try:
            return [c for c in self.checkouts if not c.returned][0]
        except:
            pass

    _repr_attr_names = ['name']

class Checkout(MagModel):
    game_id     = Column(UUID, ForeignKey('game.id'))
    game        = relationship('Game', backref=backref('checkouts', cascade='all,delete-orphan'))
    attendee_id = Column(UUID, ForeignKey('attendee.id'))
    attendee    = relationship(Attendee, backref=backref('checkouts', cascade='all,delete-orphan'))
    checked_out = Column(UTCDateTime, default=lambda: datetime.now(UTC))
    returned    = Column(UTCDateTime, nullable=True)



class PrevSeasonSupporter(MagModel):
    first_name = Column(UnicodeText)
    last_name  = Column(UnicodeText)
    email      = Column(UnicodeText)

    _repr_attr_names = ['first_name', 'last_name', 'email']

class ApprovedEmail(MagModel):
    subject = Column(UnicodeText)

    _repr_attr_names = ['subject']

class Email(MagModel):
    fk_id   = Column(UUID, nullable=True)
    model   = Column(UnicodeText)
    when    = Column(UTCDateTime, default=lambda: datetime.now(UTC))
    subject = Column(UnicodeText)
    dest    = Column(UnicodeText)
    body    = Column(UnicodeText)

    _repr_attr_names = ['subject']

    @cached_property
    def fk(self):
        try:
            return getattr(self.session, globals()[self.model].__tablename__)(self.fk_id)
        except:
            return None

    @property
    def rcpt_name(self):
        if self.model == 'Group':
            return self.fk.leader.full_name
        else:
            return self.fk.full_name

    @property
    def html(self):
        if '<body>' in self.body:
            return SafeString(self.body.split('<body>')[1].split('</body>')[0])
        else:
            return SafeString(self.body.replace('\n', '<br/>'))



class Tracking(MagModel):
    fk_id  = Column(UUID)
    model  = Column(UnicodeText)
    when   = Column(UTCDateTime, default=lambda: datetime.now(UTC))
    who    = Column(UnicodeText)
    which  = Column(UnicodeText)
    links  = Column(UnicodeText)
    action = Column(Choice(TRACKING_OPTS))
    data   = Column(UnicodeText)

    @classmethod
    def format(cls, values):
        return ', '.join('{}={}'.format(k, v) for k,v in values.items())

    @classmethod
    def repr(cls, column, value):
        try:
            s = repr(value)
            if column.name == 'hashed':
                return '<bcrypted>'
            elif isinstance(column.type, MultiChoice):
                opts = dict(column.type.choices)
                return repr('' if not value else (','.join(opts[int(opt)] for opt in value.split(',') if int(opt or 0) in opts)))
            elif isinstance(column.type, Choice) and value is not None:
                return repr(dict(column.type.choices).get(int(value), '<nonstandard>'))
            else:
                return s
        except Exception as e:
            raise ValueError('error formatting {} ({!r})'.format(column.name, value)) from e

    @classmethod
    def differences(cls, instance):
        diff = {}
        for attr, column in instance.__table__.columns.items():
            new_val = getattr(instance, attr)
            old_val = instance.orig_value_of(attr)
            if old_val != new_val:
                diff[attr] = "'{} -> {}'".format(cls.repr(column, old_val), cls.repr(column, new_val))
        return diff

    @classmethod
    def track(cls, action, instance):
        if action in [CREATED, UNPAID_PREREG, EDITED_PREREG]:
            vals = {attr: cls.repr(column, getattr(instance, attr)) for attr, column in instance.__table__.columns.items()}
            data = cls.format(vals)
        elif action == UPDATED:
            diff = cls.differences(instance)
            data = cls.format(diff)
            if len(diff) == 1 and 'badge_num' in diff:
                action = AUTO_BADGE_SHIFT
            elif not data:
                return
        elif instance == "Budget": # Vaguely horrifying special-casing where we make up fake data so we can insert this entry into the tracking DB
            data = "Budget Page"
            who = AdminAccount.admin_name() or (current_thread().name if current_thread().daemon else 'non-admin')
            with Session() as session:
                session.add(Tracking(model="Budget", fk_id=str(uuid4()),which="Budget",who=who,links='',action=action,data=data))
            return
        else:
            data = 'id={}'.format(instance.id)
        links = ', '.join(
            '{}({})'.format(list(column.foreign_keys)[0].column.table.name, getattr(instance, name))
            for name, column in instance.__table__.columns.items()
            if column.foreign_keys and getattr(instance, name)
        )
        who = AdminAccount.admin_name() or (current_thread().name if current_thread().daemon else 'non-admin')

        def _insert(session):
            session.add(Tracking(
                model = instance.__class__.__name__,
                fk_id = instance.id,
                which = repr(instance),
                who = who,
                links = links,
                action = action,
                data = data
            ))
        if instance.session:
            _insert(instance.session)
        else:
            with Session() as session:
                _insert(session)

    @classmethod
    def track_pageview(cls, url, query):
        # Track any views of the budget pages
        if "budget" in url:
            Tracking.track(PAGE_VIEWED, "Budget")
        else:
            # Only log the page view if there's a valid attendee ID
            params = dict(parse_qsl(query))
            if 'id' not in params or params['id'] == 'None':
                return

            # Looking at an attendee's details
            if "registration" in url:
                with Session() as session:
                    attendee = session.query(Attendee).filter(Attendee.id == params['id']).first()
                    Tracking.track(PAGE_VIEWED, attendee)
            # Looking at a group's details
            elif "groups" in url:
                with Session() as session:
                    group = session.query(Group).filter(Group.id == params['id']).first()
                    Tracking.track(PAGE_VIEWED, group)


Tracking.UNTRACKED = [Tracking, Email]


class Session(SessionManager):
    engine = sqlalchemy.create_engine(SQLALCHEMY_URL, pool_size=50, max_overflow=100)

    class QuerySubclass(Query):
        @property
        def is_single_table_query(self):
            return len(self.column_descriptions) == 1

        @property
        def model(self):
            assert self.is_single_table_query, 'actions such as .order() and .icontains() and .iexact() are only valid for single-table queries'
            return self.column_descriptions[0]['type']

        def order(self, attrs):
            order = []
            for attr in listify(attrs):
                col = getattr(self.model, attr.lstrip('-'))
                order.append(col.desc() if attr.startswith('-') else col)
            return self.order_by(*order)

        def icontains(self, attr=None, val=None, **filters):
            query = self
            if len(self.column_descriptions) == 1 and filters:
                for colname, val in filters.items():
                    query = query.filter(getattr(self.model, colname).ilike('%{}%'.format(val)))
            if attr and val:
                query = self.filter(attr.ilike('%{}%'.format(val)))
            return query

        def iexact(self, **filters):
            return self.filter(*[func.lower(getattr(self.model, attr)) == func.lower(val) for attr, val in filters.items()])

    class SessionMixin:
        def admin_attendee(self):
            return self.admin_account(cherrypy.session['account_id']).attendee

        def logged_in_volunteer(self):
            return self.attendee(cherrypy.session['staffer_id'])

        def checklist_status(self, slug, department):
            attendee = self.admin_attendee()
            conf = DeptChecklistConf.instances[slug]
            return {
                'conf': conf,
                'relevant': attendee.is_single_dept_head and attendee.assigned_depts_ints == [int(department or 0)],
                'completed': conf.completed(attendee)
            }

        def jobs_for_signups(self):
            fields = ['name', 'location_label', 'description', 'weight', 'start_time_local', 'duration', 'weighted_hours', 'restricted', 'extra15', 'taken']
            jobs = self.logged_in_volunteer().possible_and_current
            restricted_hours = set()
            for job in jobs:
                if job.restricted:
                    restricted_hours.add(frozenset(job.hours))
            return [job.to_dict(fields) for job in jobs if job.restricted or frozenset(job.hours) not in restricted_hours]

        def get_account_by_email(self, email):
            return self.query(AdminAccount).join(Attendee).filter(func.lower(Attendee.email) == func.lower(email)).one()
            
        def age_group_from_birthdate(self, birthdate):
            if not birthdate: return None
            calc_date = EPOCH.date() if date.today() <= EPOCH.date() else date.today()
            attendee_age = int((calc_date - birthdate).days / 365.2425)

            age_groups = self.query(AgeGroup)
            for current_age_group in age_groups:
                if current_age_group.min_age <= attendee_age <= current_age_group.max_age:
                    return current_age_group
            return None

        def no_email(self, subject):
            return not self.query(Email).filter_by(subject=subject).all()

        def season_pass(self, id):
            pss = self.query(PrevSeasonSupporter).filter_by(id=id).all()
            if pss:
                return pss[0]
            else:
                attendee = self.attendee(id)
                assert attendee.amount_extra >= SEASON_LEVEL
                return attendee

        def season_passes(self):
            attendees = {a.email: a for a in self.query(Attendee).filter(Attendee.amount_extra >= SEASON_LEVEL).all()}
            prev = [pss for pss in self.query(PrevSeasonSupporter).all() if pss.email not in attendees]
            return prev + list(attendees.values())

        def lookup_attendee(self, full_name, email, zip_code):
            words = full_name.split()
            for i in range(1, len(words)):
                first, last = ' '.join(words[:i]), ' '.join(words[i:])
                attendee = self.query(Attendee).iexact(first_name=first, last_name=last, email=email, zip_code=zip_code).all()
                if attendee:
                    return attendee[0]
            raise ValueError('attendee not found')

        def next_badge_num(self, badge_type, old_badge_num):
            #assert_badge_locked()
            badge_type = int(badge_type)

            if badge_type not in PREASSIGNED_BADGE_TYPES:
                return 0

            sametype = self.query(Attendee).filter(Attendee.badge_type == badge_type, Attendee.badge_num >= BADGE_RANGES[badge_type][0], Attendee.badge_num <= BADGE_RANGES[badge_type][1])
            if sametype.count():
                next = sametype.order_by(Attendee.badge_num.desc()).first().badge_num
                if old_badge_num and next == old_badge_num:
                    next = next # Prevents incrementing if the current badge already has the highest badge number in the range.
                else:
                    next += 1
            else:
                next = BADGE_RANGES[badge_type][0]

            # Adjusts the badge number based on badges in the session
            for attendee in [m for m in chain(self.new, self.dirty) if isinstance(m, Attendee)]:
                if attendee.badge_type == badge_type:
                    next = max(next, 1 + attendee.badge_num)

            return next

        def shift_badges(self, badge_type, badge_num, *, until=MAX_BADGE, **direction):
            #assert_badge_locked()
            assert SHIFT_CUSTOM_BADGES
            assert not any(param for param in direction if param not in ['up', 'down']), 'unknown parameters'
            assert len(direction) < 2, 'you cannot specify both up and down parameters'
            down = (not direction['up']) if 'up' in direction else direction.get('down', True)
            shift = -1 if down else 1
            for a in self.query(Attendee).filter(Attendee.badge_type == badge_type,
                                                 Attendee.badge_num >= badge_num,
                                                 Attendee.badge_num <= until,
                                                 Attendee.badge_num != 0):
                a.badge_num += shift

        def change_badge(self, attendee, badge_type, badge_num=None):
            #assert_badge_locked()
            badge_type = int(badge_type)
            old_badge_num = attendee.badge_num

            out_of_range = check_range(badge_num, badge_type)

            if out_of_range:
                return out_of_range

            # Keeps non-preassigned badges numberless unless they've already been checked in.
            if badge_type not in PREASSIGNED_BADGE_TYPES and not attendee.checked_in:
                attendee.badge_num = 0
                return 'Badge updated'

            # Badges should always be assigned a number if they're marked as pre-assigned or if they've been checked in.
            # If auto-shifting is also turned off, badge numbers cannot clobber other numbers.
            else:
                if not badge_num:
                    next = self.next_badge_num(badge_type, old_badge_num)
                    if next > BADGE_RANGES[badge_type][1]:
                        return 'There are no more badges available for that type'
                    attendee.badge_num = next
                elif not SHIFT_CUSTOM_BADGES:
                    existing = self.query(Attendee).filter_by(badge_type=badge_type, badge_num=badge_num)
                    if existing.count():
                        return 'That badge number already belongs to {!r}'.format(existing.first().full_name)
                    attendee.badge_num = badge_num
                # Here, badge number replacement is allowed, and auto-shifting is performed instead
                else:
                    next = self.next_badge_num(badge_type, old_badge_num)
                    new_badge_num = min(int(badge_num or BADGE_RANGES[badge_type][1]), next)

                    if old_badge_num < new_badge_num:
                        self.shift_badges(badge_type, old_badge_num, down=True, until=new_badge_num)
                    elif old_badge_num == new_badge_num:
                        new_badge_num += 1
                    else:
                        self.shift_badges(badge_type, new_badge_num, up=True, until=old_badge_num)
                    attendee.badge_num = new_badge_num

                    if badge_num <= next:
                        attendee.badge_type = badge_type
                        return 'That badge number was too high, so the next available badge was assigned instead'

            attendee.badge_type = badge_type
            return 'Badge updated'


        def everyone(self):
            attendees = self.query(Attendee).options(joinedload(Attendee.group)).all()
            groups = self.query(Group).options(joinedload(Group.attendees)).all()
            return attendees, groups

        def staffers(self):
            return self.query(Attendee) \
                       .filter_by(staffing=True) \
                       .options(joinedload(Attendee.group)) \
                       .order_by(Attendee.full_name)

        def single_dept_heads(self, dept=None):
            assigned = {'assigned_depts': str(dept)} if dept else {}
            return self.query(Attendee) \
                       .filter_by(ribbon=DEPT_HEAD_RIBBON, **assigned) \
                       .order_by(Attendee.full_name).all()

        def match_to_group(self, attendee, group):
            with BADGE_LOCK:
                available = [a for a in group.attendees if a.is_unassigned]
                matching = [a for a in available if a.badge_type == attendee.badge_type]
                if not available:
                    return 'The last badge for that group has already been assigned by another station'
                elif not matching:
                    return 'Badge #{} is a {} badge, but {} has no badges of that type'.format(attendee.badge_num, attendee.badge_type_label, group.name)
                else:
                    for attr in ['group', 'paid', 'amount_paid', 'ribbon']:
                        setattr(attendee, attr, getattr(matching[0], attr))
                    self.delete(matching[0])
                    self.add(attendee)
                    self.commit()

        def everything(self, location=None):
            location_filter = [Job.location == location] if location else []
            jobs = self.query(Job) \
                       .filter(*location_filter) \
                       .options(joinedload(Job.shifts)) \
                       .order_by(Job.start_time, Job.duration, Job.name).all()
            shifts = self.query(Shift) \
                         .filter(*location_filter) \
                         .options(joinedload(Shift.job), joinedload(Shift.attendee)) \
                         .join(Shift.job).order_by(Job.start_time).all()
            attendees = [a for a in self.query(Attendee)
                                        .filter_by(staffing=True)
                                        .options(joinedload(Attendee.shifts), joinedload(Attendee.group))
                                        .order_by(Attendee.full_name).all()
                         if AT_THE_CON or not location or int(location) in a.assigned_depts_ints]
            for job in jobs:
                job._available_staffers = [a for a in attendees if not job.restricted or a.trusted]
            return jobs, shifts, attendees

        def search(self, text, *filters):
            attendees = self.query(Attendee).outerjoin(Attendee.group).options(joinedload(Attendee.group)).filter(*filters)
            if ':' in text:
                target, term = text.split(':', 1)
                if target == 'email':
                    return attendees.icontains(Attendee.email, term.strip())
                elif target == 'group':
                    return attendees.icontains(Group.name, term.strip())

            terms = text.split()
            if len(terms) == 2:
                first, last = terms
                if first.endswith(','):
                    last, first = first.strip(','), last
                return attendees.icontains(first_name=first, last_name=last)
            elif len(terms) == 1 and terms[0].endswith(','):
                return attendees.icontains(last_name=terms[0].rstrip(','))
            elif len(terms) == 1 and terms[0].isdigit():
                return attendees.filter(Attendee.badge_num == terms[0])
            elif len(terms) == 1 and re.match('[a-z0-9]{8}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}', terms[0]):
                return attendees.filter(or_(Attendee.id == terms[0], Group.id == terms[0]))
            else:
                checks = [Group.name.ilike('%' + text + '%')]
                for attr in ['first_name', 'last_name', 'badge_printed_name', 'email', 'comments', 'admin_notes', 'for_review']:
                    checks.append(getattr(Attendee, attr).ilike('%' + text + '%'))
                return attendees.filter(or_(*checks))

        def delete_from_group(self, attendee, group):
            '''
            Sometimes we want to delete an attendee badge which is part of a group.  In most cases, we could just
            say "session.delete(attendee)" but sometimes we need to make sure that the attendee is ALSO removed
            from the "group.attendees" list before we commit, since the number of attendees in a group is used in
            our presave_adjustments() code to update the group price.  So anytime we delete an attendee in a group,
            we should use this method.
            '''
            self.delete(attendee)
            group.attendees.remove(attendee)

        def assign_badges(self, group, new_badge_count, new_badge_type = ATTENDEE_BADGE, **extra_create_args):
            diff = int(new_badge_count) - group.badges
            sorted_unassigned = sorted(group.floating, key=lambda a: a.registered, reverse=True)
            if diff > 0:
                for i in range(diff):
                    group.attendees.append(Attendee(badge_type=new_badge_type, ribbon=group.new_ribbon, paid=PAID_BY_GROUP, **extra_create_args))
            elif diff < 0:
                if len(group.floating) < abs(diff):
                    return 'You cannot reduce the number of badges for a group to below the number of assigned badges'
                else:
                    for attendee in sorted_unassigned[:abs(diff)]:
                        self.delete_from_group(attendee, group)

        def assign(self, attendee_id, job_id):
            job = self.job(job_id)
            attendee = self.attendee(attendee_id)

            if job.restricted and not attendee.trusted:
                return 'You cannot assign an untrusted attendee to a restricted shift'

            if job.slots <= len(job.shifts):
                return 'All slots for this job have already been filled'

            if not job.no_overlap(attendee):
                return 'This volunteer is already signed up for a shift during that time'

            self.add(Shift(attendee=attendee, job=job))
            self.commit()

        def affiliates(self):
            amounts = defaultdict(int, {a:-i for i,a in enumerate(DEFAULT_AFFILIATES)})
            for aff, amt in self.query(Attendee.affiliate, Attendee.amount_extra) \
                                .filter(and_(Attendee.amount_extra > 0, Attendee.affiliate != '')):
                amounts[aff] += amt
            return [{
                'id': aff,
                'text': aff,
                'total': max(0, amt)
            } for aff, amt in sorted(amounts.items(), key=lambda tup: -tup[1])]


def _make_getter(model):
    def getter(self, params=None, *, bools=(), checkgroups=(), allowed=(), restricted=False, ignore_csrf=False, **query):
        if query:
            return self.query(model).filter_by(**query).one()
        elif isinstance(params, str):
            return self.query(model).filter_by(id=params).one()
        else:
            params = params.copy()
            id = params.pop('id', 'None')
            if id == 'None':
                inst = model()
            else:
                inst = self.query(model).filter_by(id=id).one()

            if not ignore_csrf:
                assert not {k for k in params if k not in allowed} or cherrypy.request.method == 'POST', 'POST required'
            inst.apply(params, bools=bools, checkgroups=checkgroups, restricted=restricted, ignore_csrf=ignore_csrf)
            return inst
    return getter

for _model in Session.all_models():
    setattr(Session.SessionMixin, _model.__tablename__, _make_getter(_model))

def _presave_adjustments(session, context, instances='deprecated'):
    BADGE_LOCK.acquire()
    for model in chain(session.dirty, session.new):
        model.presave_adjustments()
    for model in session.deleted:
        model.on_delete()

def _release_badge_lock(session, context):
    try:
        BADGE_LOCK.release()
    except:
        log.error('failed releasing BADGE_LOCK after session flush; this should never actually happen, but we want to just keep going if it ever does')

def _release_badge_lock_on_error(*args, **kwargs):
    try:
        BADGE_LOCK.release()
    except:
        log.warn('failed releasing BADGE_LOCK on db error; these errors should not happen in the first place and we do not expect releasing the lock to fail when they do, but we still want to keep going if/when this does occur')

def _track_changes(session, context, instances='deprecated'):
    for action, instances in {CREATED: session.new, UPDATED: session.dirty, DELETED: session.deleted}.items():
        for instance in instances:
            if instance.__class__ not in Tracking.UNTRACKED:
                Tracking.track(action, instance)

def register_session_listeners():
    listen(Session.session_factory, 'before_flush', _presave_adjustments)
    listen(Session.session_factory, 'before_flush', _track_changes)
    listen(Session.session_factory, 'after_flush', _release_badge_lock)
    listen(Session.engine, 'dbapi_error', _release_badge_lock_on_error)
register_session_listeners()
