from uber.common import *

_checkboxes = ['staffing', 'can_spam', 'international', 'no_cellphone']

def to_sessionized(attendee, group):
    if group.badges:
        return Charge.to_sessionized(group)
    else:
        return Charge.to_sessionized(attendee)

def check_prereg_reqs(attendee):
    if attendee.badge_type == PSEUDO_DEALER_BADGE and not attendee.cellphone:
        return 'Your phone number is required'
    elif attendee.amount_extra >= SHIRT_LEVEL and attendee.shirt == NO_SHIRT:
        return 'Your shirt size is required'

def check_dealer(group):
    if not group.address and COLLECT_INTERESTS:
        return 'Dealers are required to provide an address for tax purposes'
    elif not group.wares:
        return 'You must provide a detailed explanation of what you sell for us to evaluate your submission'
    elif not group.website:
        if COLLECT_INTERESTS:
            return "Please enter your business' website address"
        else:
            return "You must enter a Pennsylvania Tax ID for your dealership"
    elif not group.description:
        return "Please provide a brief description of your business"

def send_banned_email(attendee):
    try:
        send_email(REGDESK_EMAIL, REGDESK_EMAIL, 'Banned attendee registration',
                   render('emails/reg_workflow/banned_attendee.txt', {'attendee': attendee}), model='n/a')
    except:
        log.error('unable to send banned email about {}', attendee)

@all_renderable()
class Root:
    @property
    def unpaid_preregs(self):
        return cherrypy.session.setdefault('unpaid_preregs', OrderedDict())

    @property
    def paid_preregs(self):
        return cherrypy.session.setdefault('paid_preregs', [])

    def _get_unsaved(self, id, if_not_found=HTTPRedirect('index')):
        if id in self.unpaid_preregs:
            target = Charge.from_sessionized(self.unpaid_preregs[id])
            if isinstance(target, Attendee):
                return target, Group()
            else:
                [leader] = [a for a in target.attendees if not a.is_unassigned]
                return leader, target
        else:
            raise if_not_found

    def stats(self):
        cherrypy.response.headers["Access-Control-Allow-Origin"] = "*"
        return json.dumps({
            'badges_sold': state.BADGES_SOLD,
            'remaining_badges': max(0, MAX_BADGE_SALES - state.BADGES_SOLD),

            'server_current_timestamp': int(datetime.utcnow().timestamp()),
            'warn_if_server_browser_time_mismatch': WARN_IF_SERVER_BROWSER_TIME_MISMATCH
        })

    def check_prereg(self):
        return json.dumps({'force_refresh': state.AFTER_PREREG_TAKEDOWN or state.BADGES_SOLD >= MAX_BADGE_SALES})

    def check_if_preregistered(self, session, message="", **params):
        if 'email' in params:
            attendee = session.query(Attendee).filter(func.lower(Attendee.email) == func.lower(params['email'])).first()
            message = "Thank you! You will receive a confirmation email if you are registered for "+EVENT_NAME_AND_YEAR+"."
            subject = EVENT_NAME_AND_YEAR+' Registration Confirmation'

            if attendee:
                last_email = session.query(Email)\
                                  .filter(and_(Email.dest == attendee.email, Email.subject == subject))\
                                  .order_by(Email.when.desc()).first()
                if not last_email or last_email.when < (localized_now() - timedelta(days=7)):
                    send_email(REGDESK_EMAIL, attendee.email, subject, render('emails/reg_workflow/prereg_check.html', {
                        'attendee': attendee }), model=attendee)
        return {'message': message}


    @check_if_can_reg
    def index(self, message=''):
        if not self.unpaid_preregs:
            raise HTTPRedirect('form?message={}', message) if message else HTTPRedirect('form')
        else:
            return {
                'message': message,
                'charge': Charge(listify(self.unpaid_preregs.values()))
            }
              
    @check_if_can_reg
    def badge_choice(self, message=''):
        return {'message': message}
        
    @check_if_can_reg
    def dealer_registration(self, message=''):
        return self.form(badge_type=PSEUDO_DEALER_BADGE, message=message)

    @check_if_can_reg
    def form(self, session, message='', edit_id=None, payment_override=None, **params):
        if MODE == 'magstock':
            if params.get('buy_shirt') != 'on':
                params['shirt'] = NO_SHIRT
                params['shirt_color'] = NO_SHIRT

        params['id'] = 'None'   # security!
        if edit_id is not None:
            attendee, group = self._get_unsaved(edit_id, if_not_found=HTTPRedirect('form?message={}', 'That preregistration has already been finalized'))
            attendee.apply(params, bools=_checkboxes)
            group.apply(params)
            params.setdefault('badges', group.badges)
        else:
            attendee = session.attendee(params, bools=_checkboxes, ignore_csrf=True, restricted=True)
            group = session.group(params, ignore_csrf=True, restricted=True)

        if not attendee.badge_type:
            attendee.badge_type = ATTENDEE_BADGE
        if attendee.badge_type not in state.PREREG_BADGE_TYPES:
            raise HTTPRedirect('form?message={}', 'Invalid badge type!')
            
        if attendee.is_dealer and not state.DEALER_REG_OPEN:
            return render('static_views/dealer_reg_closed.html') if state.AFTER_DEALER_REG_SHUTDOWN else render('static_views/dealer_reg_not_open.html')
            
        if 'first_name' in params:
            message = check(attendee) or check_prereg_reqs(attendee)
            if not message and attendee.badge_type in [PSEUDO_DEALER_BADGE, PSEUDO_GROUP_BADGE]:
                message = check(group)
                if attendee.badge_type == PSEUDO_DEALER_BADGE:
                    message = check_dealer(group)
                    if int(params['badges']) > MAX_DEALER_BADGES.get(float(params['tables']), 1):
                        message = "Too many dealer assistants!"

            if not message:
                if attendee.badge_type in [PSEUDO_DEALER_BADGE, PSEUDO_GROUP_BADGE]:
                    attendee.paid = PAID_BY_GROUP
                    group.attendees = [attendee]
                    session.assign_badges(group, params['badges'])
                    if attendee.badge_type == PSEUDO_GROUP_BADGE:
                        group.tables = 0
                    elif attendee.badge_type == PSEUDO_DEALER_BADGE:
                        group.status = WAITLISTED if state.AFTER_DEALER_REG_DEADLINE else UNAPPROVED
                        attendee.ribbon = DEALER_RIBBON

                if attendee.is_dealer:
                    session.add_all([attendee, group])
                    session.commit()
                    send_email(MARKETPLACE_EMAIL, MARKETPLACE_EMAIL, 'Dealer Application Received',
                               render('emails/dealers/reg_notification.txt', {'group': group}), model=group)
                    send_email(MARKETPLACE_EMAIL, group.leader.email, 'Dealer Application Received',
                               render('emails/dealers/dealer_received.txt', {'group': group}), model=group)
                    raise HTTPRedirect('dealer_confirmation?id={}', group.id)
                else:
                    target = group if group.badges else attendee
                    track_type = EDITED_PREREG if target.id in self.unpaid_preregs else UNPAID_PREREG
                    self.unpaid_preregs[target.id] = to_sessionized(attendee, group)
                    Tracking.track(track_type, attendee)
                    if group.badges:
                        Tracking.track(track_type, group)

                if AT_THE_CON and attendee.payment_method != STRIPE or payment_override is not None:
                    new_attendees = Charge(listify(self.unpaid_preregs.values()))
                    for attendee in new_attendees.attendees:
                        session.add(attendee)
                        attendee.badge_num = 0
                        if not attendee.zip_code:
                            attendee.zip_code = '00000'
                        if payment_override: attendee.payment_method = payment_override
                        session.commit()
                    self.unpaid_preregs.clear()
                    message = 'Thanks!  Please queue in the {} line and have your photo ID and {} ready.'
                    if attendee.payment_method == GROUP:
                        message = 'Please proceed to the preregistration line to pick up your badge.'
                        attendee.paid = PAID_BY_GROUP
                    elif attendee.payment_method == CASH:
                        message = message.format('cash', '${}'.format(attendee.total_cost))
                    elif attendee.payment_method == MANUAL:
                        message = message.format('credit card', 'credit card')
                    raise HTTPRedirect('form?message={}', message)
                else:
                    if session.query(Attendee).filter_by(first_name=attendee.first_name, last_name=attendee.last_name, email=attendee.email).count():
                        raise HTTPRedirect('duplicate?id={}', group.id if attendee.paid == PAID_BY_GROUP else attendee.id)

                    if attendee.full_name in BANNED_ATTENDEES:
                        raise HTTPRedirect('banned?id={}', group.id if attendee.paid == PAID_BY_GROUP else attendee.id)

                    raise HTTPRedirect('index')
        else:
            if edit_id is None:
                attendee.can_spam = True    # only defaults to true for these forms
                attendee.payment_method = STRIPE # our preferred payment method, and makes it easier when registering groups
            if attendee.badge_type == PSEUDO_DEALER_BADGE and state.AFTER_DEALER_REG_DEADLINE:
                message = 'Dealer registration is closed, but you can fill out this form to add yourself to our waitlist'

        return {
            'message':    message,
            'attendee':   attendee,
            'group':      group,
            'edit_id':    edit_id,
            'badges':     params.get('badges'),
            'affiliates': session.affiliates()
        }

    def duplicate(self, session, id):
        attendee, group = self._get_unsaved(id)
        orig = session.query(Attendee).filter_by(first_name=attendee.first_name, last_name=attendee.last_name, email=attendee.email).first()
        if not orig:
            raise HTTPRedirect('index')

        return {
            'duplicate': attendee,
            'attendee': orig
        }

    def banned(self, id):
        attendee, group = self._get_unsaved(id)
        return {'attendee': attendee}

    @credit_card
    def prereg_payment(self, session, payment_id, stripeToken):
        charge = Charge.get(payment_id)
        if not charge.total_cost:
            message = 'Your preregistration has already been paid for, so your credit card has not been charged'
        elif charge.amount != charge.total_cost:
            message = 'Our preregistration price has gone up; please fill out the payment form again at the higher price'
        else:
            message = charge.charge_cc(stripeToken)

        if message:
            raise HTTPRedirect('index?message={}', message)

        for attendee in charge.attendees:
            attendee.paid = HAS_PAID
            attendee.amount_paid = attendee.total_cost
            session.add(attendee)
            if attendee.full_name in BANNED_ATTENDEES:
                send_banned_email(attendee)

        for group in charge.groups:
            group.amount_paid = group.default_cost - group.amount_extra
            for attendee in group.attendees:
                if attendee.amount_extra:
                    attendee.amount_paid = attendee.amount_extra
            session.add(group)
            session.commit()  # commit now so group.leader will resolve
            if group.leader.full_name in BANNED_ATTENDEES:
                send_banned_email(group.leader)

        self.unpaid_preregs.clear()
        self.paid_preregs.extend(charge.targets)
        raise HTTPRedirect('paid_preregistrations?payment_received={}', charge.dollar_amount)

    def paid_preregistrations(self, session, payment_received=None):
        if not self.paid_preregs:
            raise HTTPRedirect('index')
        else:
            preregs = [session.merge(Charge.from_sessionized(d)) for d in self.paid_preregs]
            for prereg in preregs:
                try:
                    session.refresh(prereg)
                except:
                    pass  # this badge must have subsequently been transferred or deleted
            return {
                'preregs': preregs,
                'total_cost': payment_received
            }

    def delete(self, id):
        self.unpaid_preregs.pop(id, None)
        raise HTTPRedirect('index?message={}', 'Preregistration deleted')

    def dealer_confirmation(self, session, id):
        return {'group': session.group(id)}

    def group_members(self, session, id, message=''):
        group = session.group(id)
        return {
            'group':   group,
            'charge':  Charge([group, group.leader]),  # Include group leader's kick-in level if not paid-for
            'message': message
        }

    def register_group_member(self, session, group_id, message='', **params):
        group = session.group(group_id)
        attendee = session.attendee(params, bools=_checkboxes, restricted=True)
        if 'first_name' in params:
            message = check(attendee) or check_prereg_reqs(attendee)
            if not message and not params['first_name']:
                message = 'First and Last Name are required fields'
            if not message:
                if not group.floating:
                    raise HTTPRedirect('group_members?id={}&message={}', group_id, 'No more unassigned badges exist in this group')

                if attendee.full_name in BANNED_ATTENDEES:
                    send_banned_email(attendee)

                badge_being_claimed = group.floating[0]

                # Free group badges are only considered 'registered' when they are actually claimed.
                if group.cost == 0:
                    attendee.registered = localized_now()
                else:
                    attendee.registered = badge_being_claimed.registered

                attendee.badge_type = badge_being_claimed.badge_type
                attendee.ribbon = badge_being_claimed.ribbon
                session.delete_from_group(badge_being_claimed, group)

                group.attendees.append(attendee)
                attendee.paid = PAID_BY_GROUP
                session.add(attendee)
                if attendee.amount_unpaid:
                    raise HTTPRedirect('group_extra_payment_form?id={}', attendee.id)
                else:
                    raise HTTPRedirect('group_members?id={}&message={}', group.id, 'Badge registered successfully')
        else:
            attendee.can_spam = True    # only defaults to true for these forms

        return {
            'message':  message,
            'group_id': group_id,
            'group': group,
            'attendee': attendee,
            'affiliates': session.affiliates()
        }

    def group_extra_payment_form(self, session, id):
        attendee = session.attendee(id)
        return {
            'attendee': attendee,
            'charge':   Charge(attendee, description = '{} kicking in extra'.format(attendee.full_name))
        }

    def group_undo_extra_payment(self, session, id):
        attendee = session.attendee(id)
        attendee.amount_extra -= attendee.amount_unpaid
        raise HTTPRedirect('group_members?id={}&message={}', attendee.group_id, 'Extra payment undone')

    @credit_card
    def process_group_payment(self, session, payment_id, stripeToken):
        charge = Charge.get(payment_id)
        [group] = charge.groups
        [attendee] = charge.attendees
        message = charge.charge_cc(stripeToken)
        if message:
            raise HTTPRedirect('group_members?id={}&message={}', group.id, message)
        else:
            group.amount_paid += charge.dollar_amount

            # Subtract an attendee's kick-in level, if it's not already paid for.
            if attendee.amount_paid < attendee.total_cost:
                group.amount_paid -= attendee.total_cost - attendee.amount_paid
            
            attendee.amount_paid = attendee.total_cost
            if group.tables:
                send_email(MARKETPLACE_EMAIL, MARKETPLACE_EMAIL, 'Dealer Payment Completed',
                           render('emails/dealers/payment_notification.txt', {'group': group}), model=group)
            session.merge(group)
            session.merge(attendee)
            raise HTTPRedirect('group_members?id={}&message={}', group.id, 'Your payment has been accepted!')

    @credit_card
    def process_group_member_payment(self, session, payment_id, stripeToken):
        charge = Charge.get(payment_id)
        [attendee] = charge.attendees
        attendee = session.merge(attendee)
        message = charge.charge_cc(stripeToken)
        if message:
            attendee.amount_extra -= attendee.amount_unpaid
            raise HTTPRedirect('group_members?id={}&message={}', attendee.group_id, message)
        else:
            attendee.amount_paid += charge.dollar_amount
            raise HTTPRedirect('group_members?id={}&message={}', attendee.group_id, 'Extra payment accepted')

    @csrf_protected
    def unset_group_member(self, session, id):
        attendee = session.attendee(id)
        try:
            send_email(REGDESK_EMAIL, attendee.email, '{EVENT_NAME} group registration dropped',
                       render('emails/reg_workflow/group_member_dropped.txt', {'attendee': attendee}), model=attendee)
        except:
            log.error('unable to send group unset email', exc_info=True)

        session.assign_badges(attendee.group, attendee.group.badges + 1)
        Tracking.track(INVALIDATED, attendee)
        #session.delete_from_group(attendee, attendee.group)
        attendee.group.attendees.remove(attendee)
        raise HTTPRedirect('group_members?id={}&message={}', attendee.group_id, 'Attendee unset; you may now assign their badge to someone else')

    def add_group_members(self, session, id, count):
        group = session.group(id)
        if int(count) < group.min_badges_addable:
            raise HTTPRedirect('group_members?id={}&message={}', group.id, 'This group cannot add fewer than {} badges'.format(group.min_badges_addable))

        charge = Charge(group, amount = 100 * int(count) * state.GROUP_PRICE, description = '{} extra badges for {}'.format(count, group.name))
        return {
            'count': count,
            'group': group,
            'charge': charge
        }

    @credit_card
    def pay_for_extra_members(self, session, payment_id, stripeToken):
        charge = Charge.get(payment_id)
        [group] = charge.groups
        badges_to_add = charge.dollar_amount // state.GROUP_PRICE
        if charge.dollar_amount % state.GROUP_PRICE:
            message = 'Our preregistration price has gone up since you tried to add the badges; please try again'
        else:
            message = charge.charge_cc(stripeToken)

        if message:
            raise HTTPRedirect('group_members?id={}&message={}', group.id, message)
        else:
            session.assign_badges(group, group.badges + badges_to_add)
            group.amount_paid += charge.dollar_amount
            session.merge(group)
            raise HTTPRedirect('group_members?id={}&message={}', group.id, 'You payment has been accepted and the badges have been added to your group')

    def transfer_badge(self, session, message='', **params):
        old = session.attendee(params['id'])
        assert old.is_transferable, 'This badge is not transferrable'
        session.expunge(old)
        attendee = session.attendee(params, bools=_checkboxes, restricted=True)

        if 'first_name' in params:
            message = check(attendee) or check_prereg_reqs(attendee)
            if not message and (not params['first_name'] and not params['last_name']):
                message = 'First and Last names are required.'
            if not message:
                subject, body = EVENT_NAME + ' Registration Transferred', render('emails/reg_workflow/badge_transfer.txt', {'new': attendee, 'old': old})
                try:
                    send_email(REGDESK_EMAIL, [old.email, attendee.email, REGDESK_EMAIL], subject, body, model=attendee)
                except:
                    log.error('unable to send badge change email', exc_info = True)

                if attendee.full_name in BANNED_ATTENDEES:
                    send_banned_email(attendee)

                if attendee.group_id:
                    raise HTTPRedirect('group_members?id={}&message={}', attendee.group_id, 'Registration successfully transferred')
                else:
                    raise HTTPRedirect('confirm?id={}&message={}', attendee.id, 'Your registration has been transferred')
        else:
            for attr in ['first_name','last_name','email','zip_code','international','ec_phone','cellphone','interests','age_group','staffing','requested_depts']:
                setattr(attendee, attr, getattr(Attendee(), attr))

        return {
            'old':      old,
            'attendee': attendee,
            'message':  message
        }

    def confirm(self, session, message='', return_to='confirm', **params):
        attendee = session.attendee(params, bools=_checkboxes, restricted=True)

        placeholder = attendee.placeholder
        if 'email' in params:
            attendee.placeholder = False
            message = check(attendee) or check_prereg_reqs(attendee)
            if not message:
                if placeholder:
                    message = 'Your registration has been confirmed.'
                else:
                    message = 'Your information has been updated.'

                page = ('confirm?id=' + attendee.id + '&') if return_to == 'confirm' else (return_to + '?')
                if attendee.amount_unpaid:
                    cherrypy.session['return_to'] = page
                    raise HTTPRedirect('attendee_donation_form?id={}', attendee.id)
                else:
                    raise HTTPRedirect(page + 'message=' + message)

        elif attendee.amount_unpaid and attendee.zip_code:  # don't skip to payment until the form is filled out
            raise HTTPRedirect('attendee_donation_form?id={}&message={}', attendee.id, message)

        attendee.placeholder = placeholder
        if not message and attendee.placeholder:
            message = 'You are not yet registered!  You must fill out this form to complete your registration.'
        elif not message:
            message = 'You are already registered but you may update your information with this form.'

        return {
            'return_to':  return_to,
            'attendee':   attendee,
            'message':    message,
            'affiliates': session.affiliates()
        }

    def guest_food(self, session, id):
        attendee = session.attendee(id)
        assert attendee.badge_type == GUEST_BADGE, 'This form is for guests only'
        cherrypy.session['staffer_id'] = attendee.id
        raise HTTPRedirect('../signups/food_restrictions')

    def attendee_donation_form(self, session, id, message=''):
        attendee = session.attendee(id)
        return {
            'message': message,
            'attendee': attendee,
            'charge': Charge(attendee, description = '{}{}'.format(attendee.full_name, '' if attendee.overridden_price else ' kicking in extra'))
        }

    def undo_attendee_donation(self, session, id):
        attendee = session.attendee(id)
        attendee.amount_extra = max(0, attendee.amount_extra - attendee.amount_unpaid)
        raise HTTPRedirect(cherrypy.session.pop('return_to', 'confirm?id=' + id))

    @credit_card
    def process_attendee_donation(self, session, payment_id, stripeToken):
        charge = Charge.get(payment_id)
        [attendee] = charge.attendees
        message = charge.charge_cc(stripeToken)
        return_to = cherrypy.session.pop('return_to', 'confirm?id=' + attendee.id + '&') + 'message={}'
        if message:
            raise HTTPRedirect(return_to, message)
        else:
            attendee.amount_paid += charge.dollar_amount
            if attendee.paid == NOT_PAID and attendee.amount_paid == attendee.total_cost:
                attendee.paid = HAS_PAID
            session.merge(attendee)
            raise HTTPRedirect(return_to, 'Your payment has been accepted, thanks so much!')

    def event(self, session, id, slug, register=None):
        season_pass = session.season_pass(id)
        event = SeasonEvent.instances[slug]
        deadline_passed = localized_now() > event.deadline
        if register and not deadline_passed:
            session.add(SeasonPassTicket(fk_id=season_pass.id, slug=slug))
            raise HTTPRedirect('event?id={}&slug={}', id, slug)

        return {
            'event': event,
            'attendee': season_pass,
            'deadline_passed': deadline_passed,
            'registered': bool(session.query(SeasonPassTicket).filter_by(fk_id=id, slug=slug).count())
        }

    def credit_card_retry(self):
        return {}

if POST_CON:
    @all_renderable()
    class Root:
        def default(self, *args, **kwargs):
            return """
                <html><head></head><body style='text-align:center'>
                    <h2 style='color:red'>Hope you had a great MAGFest!</h2>
                    Preregistration for MAGFest 13 will open in the summer.
                </body></html>
            """

        # TODO: figure out if this is the best way to handle the issue of people not getting shirts
        def shirt_reorder(self, session, message = '', **params):
            attendee = session.attendee(params, restricted = True)
            assert attendee.owed_shirt, "There's no record of {} being owed a tshirt".format(attendee.full_name)
            if 'address' in params:
                if attendee.shirt in [NO_SHIRT, SIZE_UNKNOWN]:
                    message = 'Please select a shirt size.'
                elif not attendee.address:
                    message = 'Your address is required.'
                else:
                    raise HTTPRedirect('shirt?id={}', attendee.id)
            elif attendee.address:
                message = "We've recorded your shirt size and address, which you may update anytime before Jan 31st."

            return {
                'message': message,
                'attendee': attendee
            }
