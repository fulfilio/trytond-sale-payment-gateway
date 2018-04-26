# -*- coding: utf-8 -*-
from decimal import Decimal

from trytond.model import ModelView, fields, Workflow
from trytond.pool import PoolMeta, Pool
from trytond.transaction import Transaction
from trytond.pyson import Eval, Bool, And, Not, Or, If
from trytond.wizard import Wizard, StateView, StateTransition, Button

from trytond.modules.payment_gateway.transaction import BaseCreditCardViewMixin

__all__ = ['Sale', 'PaymentTransaction', 'AddSalePaymentView', 'AddSalePayment']
__metaclass__ = PoolMeta

READONLY_STATES = {
    'readonly': Eval('state').in_(['cancel', 'processing', 'done'])
}
DEPENDS = ['state']


class Sale:
    'Sale'
    __name__ = 'sale.sale'

    # Readonly because the wizard should be the one adding payment gateways as
    # it provides a more cusomizable UX than directly adding a record.
    # For example, taking CC numbers.
    payments = fields.One2Many(
        'sale.payment', 'sale', 'Payments', states={
            'readonly': Eval('state').in_(['done', 'cancel'])
        },
    )
    sorted_payments = fields.Function(
        fields.One2Many('sale.payment', None, 'Payments'),
        'get_sorted_payments'
    )

    # Sale must be able to define when it should authorize and capture the
    # payments.
    payment_authorize_on = fields.Selection(
        'get_authorize_options', 'Authorize payments', required=True,
        states=READONLY_STATES, depends=DEPENDS
    )
    payment_capture_on = fields.Selection(
        'get_capture_options', 'Capture payments', required=True,
        states=READONLY_STATES, depends=DEPENDS
    )

    gateway_transactions = fields.Function(
        fields.One2Many(
            'payment_gateway.transaction', None, 'Gateway Transactions',
        ), "get_gateway_transactions"
    )
    payment_total = fields.Function(
        fields.Numeric(
            'Total Payment', digits=(16, Eval('currency_digits', 2)),
            depends=['currency_digits'],
            help="Total value of payments"
        ), 'get_payment',
    )
    payment_collected = fields.Function(
        fields.Numeric(
            'Payment Collected', digits=(16, Eval('currency_digits', 2)),
            depends=['currency_digits'],
            help="Total value of payments collected"
        ), 'get_payment',
    )
    payment_available = fields.Function(
        fields.Numeric(
            'Payment Remaining', digits=(16, Eval('currency_digits', 2)),
            depends=['currency_digits'],
            help="Total value which is neither authorize nor captured"
        ), 'get_payment',
    )
    payment_authorized = fields.Function(
        fields.Numeric(
            'Payment Authorized', digits=(16, Eval('currency_digits', 2)),
            depends=['currency_digits'],
            help="Amount authorized to be catured"
        ), 'get_payment',
    )
    payment_captured = fields.Function(
        fields.Numeric(
            'Payment Captured', digits=(16, Eval('currency_digits', 2)),
            depends=['currency_digits'],
            help="Amount already captured"
        ), 'get_payment',
    )
    payment_processing_state = fields.Selection([
        (None, 'None'),
        ('waiting_for_auth', 'Waiting For Authorization'),
        ('waiting_for_capture', 'Waiting For Capture'),
    ], "Payment Processing State", select=True, readonly=True)

    @staticmethod
    def default_payment_processing_state():
        return None

    @classmethod
    def __setup__(cls):
        super(Sale, cls).__setup__()
        cls._buttons.update({
            'add_payment': {
                'invisible': Eval('state').in_(['cancel', 'draft']),
            },
            'auth_capture': {
                'invisible': Eval('state').in_(['cancel', 'draft', 'done']),
            },
        })
        cls._error_messages.update({
            'insufficient_amount_to_authorize':
                "Insufficient amount remaining in payment\n"
                "Amount to authorize: %s\n"
                "Amount remaining: %s\n"
                "Payments: %s",
            'insufficient_amount_to_capture':
                "Insufficient amount remaining in payment\n"
                "Amount to capture: %s\n"
                "Amount remaining: %s\n"
                "Payments: %s",
            "auth_before_capture":
                "Payment authorization must happen before capture",
            "sale_payments_waiting": "Sale Payments are %s",
        })

    @classmethod
    def validate(cls, sales):
        super(Sale, cls).validate(sales)

        for sale in sales:
            sale.validate_payment_combination()

    def validate_payment_combination(self):
        if self.payment_authorize_on == 'sale_process' and \
                self.payment_capture_on == 'sale_confirm':
            self.raise_user_error("auth_before_capture")

    @classmethod
    def get_authorize_options(cls):
        """Return all the options from sale configuration.
        """
        SaleConfiguration = Pool().get('sale.configuration')

        return SaleConfiguration.get_authorize_options()

    @classmethod
    def get_capture_options(cls):
        """Return all the options from sale configuration.
        """
        SaleConfiguration = Pool().get('sale.configuration')

        return SaleConfiguration.get_capture_options()

    @staticmethod
    def default_payment_authorize_on():
        SaleConfiguration = Pool().get('sale.configuration')

        return SaleConfiguration(1).payment_authorize_on

    @staticmethod
    def default_payment_capture_on():
        SaleConfiguration = Pool().get('sale.configuration')

        return SaleConfiguration(1).payment_capture_on

    @classmethod
    def get_payment_method_priority(cls):
        """Priority order for payment methods. Downstream modules can override
        this method to change the method priority.
        """
        return ('manual', 'credit_card')

    def get_gateway_transactions(self, name):
        GatewayTransaction = Pool().get('payment_gateway.transaction')

        return map(
            int, GatewayTransaction.search(
                [('sale_payment', 'in', map(int, self.payments))]
            )
        )

    def get_payment(self, name):
        """Return amount from payments.
        """
        Payment = Pool().get('sale.payment')

        payments = Payment.search([('sale', '=', self.id)])

        if name == 'payment_total':
            return Decimal(sum([payment.amount for payment in payments]))

        elif name == 'payment_available':
            return Decimal(
                sum([payment.amount_available for payment in payments])
            )

        elif name == 'payment_captured':
            return Decimal(sum(
                [payment.amount_captured for payment in payments]
            ))

        elif name == 'payment_authorized':
            return Decimal(sum(
                [payment.amount_authorized for payment in payments]
            ))

        elif name == 'payment_collected':
            return self.payment_total - self.payment_available

    @classmethod
    @ModelView.button_action('sale_payment_gateway.wizard_add_payment')
    def add_payment(cls, sales):
        pass

    def get_sorted_payments(self, name=None):
        """
        Return the payments in the order they should be consumed
        """
        payment_method_priority = self.get_payment_method_priority()
        return map(int, sorted(
            self.payments,
            key=lambda t: payment_method_priority.index(t.method)
        ))

    def _raise_sale_payments_waiting(self):
        Sale = Pool().get('sale.sale')

        self.raise_user_error(
            "sale_payments_waiting", (
                dict(Sale.payment_processing_state.selection).get(
                    self.payment_processing_state
                ),
            )
        )

    def authorize_payments(
            self, amount, description="Payment from sale", threshold=0):
        """
        Authorize sale payments. It actually creates payment transactions
        corresponding to sale payments and set the payment processing state to
        `waiting to auth`.
        """
        if self.payment_processing_state:
            self._raise_sale_payments_waiting()

        if (amount - self.payment_available) > threshold:
            self.raise_user_error(
                "insufficient_amount_to_authorize", error_args=(
                    amount,
                    self.payment_available,
                    len(self.payments),
                )
            )

        transactions = []
        for payment in self.sorted_payments:
            if not amount:
                break

            if not payment.amount_available or payment.method == "manual":
                # * if no amount available, continue to next.
                # * manual payment need not to be authorized.
                continue

            # The amount to authorize is the amount_available if the
            # amount_available is less than the amount we seek.
            authorize_amount = min(amount, payment.amount_available)

            payment_transaction = payment._create_payment_transaction(
                authorize_amount, description
            )
            payment_transaction.save()

            amount -= authorize_amount

            transactions.append(payment_transaction)

        self.payment_processing_state = "waiting_for_auth"
        self.save()

        return transactions

    def capture_payments(
            self, amount, description="Payment from sale", threshold=0):
        """Capture sale payments.

        * If existing authorizations exist, capture them
        * If not, capture available payments directly
        """
        if self.payment_processing_state:
            self._raise_sale_payments_waiting()

        if (amount - self.payment_available - self.payment_authorized) > \
                threshold:
            self.raise_user_error(
                "insufficient_amount_to_capture", error_args=(
                    amount,
                    self.payment_available,
                    len(self.payments),
                )
            )

        transactions = []
        authorized_transactions = filter(
            lambda transaction: transaction.state == 'authorized',
            self.gateway_transactions
        )
        for transaction in authorized_transactions:
            if not amount:
                break       # pragma: no cover

            capture_amount = min(amount, transaction.amount)

            # Write the new amount of the transaction as the amount
            # required to be captured
            transaction.amount = capture_amount
            transaction.save()

            amount -= capture_amount

            transactions.append(transaction)

        for payment in self.sorted_payments:
            if not amount:
                break

            if not payment.amount_available:
                continue

            # The amount to capture is the amount_available if the
            # amount_available is less than the amount we seek.
            authorize_amount = min(amount, payment.amount_available)

            payment_transaction = payment._create_payment_transaction(
                authorize_amount, description
            )
            payment_transaction.save()

            amount -= authorize_amount

            transactions.append(payment_transaction)

        self.payment_processing_state = "waiting_for_capture"
        self.save()

        return transactions

    @classmethod
    def auth_capture(cls, sales):
        """
        A button triggered version of authorizing or capturing payment directly
        from an order.
        """
        for sale in sales:
            if sale.state == 'confirmed':
                sale.handle_payment_on_confirm()
            elif sale.state == 'processing':
                sale.handle_payment_on_process()
            sale.process_pending_payments()

    def handle_payment_on_confirm(self):
        if self.payment_capture_on == 'sale_confirm':
            self.capture_payments(
                self.total_amount - self.payment_captured
            )

        elif self.payment_authorize_on == 'sale_confirm':
            self.authorize_payments(
                self.total_amount - self.payment_authorized
            )

    def handle_payment_on_process(self):
        if self.payment_capture_on == 'sale_process':
            self.capture_payments(
                self.total_amount - self.payment_captured
            )

        elif self.payment_authorize_on == 'sale_process':
            self.authorize_payments(
                self.total_amount - self.payment_authorized
            )

    def settle_manual_payments(self):
        """
        Manual payments should be settled when the order is processed. This is
        separated into a different method so downstream modules can change this
        behavior to adapt to different workflows
        """
        for payment in self.payments:
            if payment.amount_available and payment.method == "manual" and \
                    not payment.payment_transactions:
                payment_transaction = payment._create_payment_transaction(
                    payment.amount_available,
                    'Post manual payments on Processing Order',
                )
                payment_transaction.save()
                payment.capture()

    @classmethod
    @ModelView.button
    @Workflow.transition('confirmed')
    def confirm(cls, sales):
        super(Sale, cls).confirm(sales)

        for sale in sales:
            sale.handle_payment_on_confirm()

    @classmethod
    def process(cls, sales):
        for sale in sales:
            if sale.state != 'confirmed':
                continue
            sale.handle_payment_on_process()
            sale.settle_manual_payments()
        super(Sale, cls).process(sales)

    def _pay_using_credit_card(self, gateway, credit_card, amount):
        '''
        Complete using the given credit card and finish the transaction.
        :param gateway: Active record of the payment gateway to process card
        :param credit_card: A dictionary with either of the following
                            information sets:
                            * owner: name of the owner (unicode)
                            * number: number of the credit card
                            * expiry_month: expiry month (int or string)
                            * expiry_year: year as string
                            * cvv: the cvv number
                            In future this method will accept track1 and track2
                            as valid information.
        :param amount: Decimal amount to charge the card for
        '''
        TransactionUseCardWizard = Pool().get(
            'payment_gateway.transaction.use_card', type='wizard'
        )
        PaymentTransaction = Pool().get('payment_gateway.transaction')

        # Manual card based operation
        payment_transaction = PaymentTransaction(
            party=self.party,
            address=self.invoice_address,
            amount=amount,
            currency=self.currency,
            gateway=gateway,
            sale=self,
        )
        payment_transaction.save()

        use_card_wiz = TransactionUseCardWizard(
            TransactionUseCardWizard.create()[0]        # Wizard session
        )
        use_card_wiz.card_info.owner = credit_card['owner']
        use_card_wiz.card_info.number = credit_card['number']
        use_card_wiz.card_info.expiry_month = credit_card['expiry_month']
        use_card_wiz.card_info.expiry_year = credit_card['expiry_year']
        use_card_wiz.card_info.csc = credit_card['cvv']

        with Transaction().set_context(active_id=payment_transaction.id):
            use_card_wiz.transition_capture()

    def _pay_using_profile(self, payment_profile, amount):
        '''
        Complete the Checkout using a payment_profile. Only available to the
        registered users of the website.
        :param payment_profile: Active record of payment profile
        :param amount: Decimal amount to charge the card for
        '''
        PaymentTransaction = Pool().get('payment_gateway.transaction')

        if payment_profile.party != self.party:
            self.raise_user_error(
                "Payment profile'd owner is %s, but the customer is %s" % (
                    payment_profile.party.name,
                    self.party.name,
                )
            )

        payment_transaction = PaymentTransaction(
            party=self.party,
            address=self.invoice_address,
            payment_profile=payment_profile,
            amount=amount,
            currency=self.currency,
            gateway=payment_profile.gateway,
            sale=self,
        )
        payment_transaction.save()

        PaymentTransaction.capture([payment_transaction])

    def process_pending_payments(self):
        """Process waiting payments for corresponding sale.
        """
        PaymentTransaction = Pool().get('payment_gateway.transaction')

        if self.payment_processing_state == "waiting_for_auth":
            for payment in self.sorted_payments:
                payment.authorize()
            self.payment_processing_state = None

        elif self.payment_processing_state == "waiting_for_capture":
            # Transactions waiting for capture.
            txns = PaymentTransaction.search([
                ('sale_payment.sale', '=', self.id),
            ])

            # Settle authorized transactions
            PaymentTransaction.settle(filter(
                lambda txn: txn.state == 'authorized', txns
            ))

            # Capture other transactions
            PaymentTransaction.capture(filter(
                lambda txn: txn.state == "draft", txns
            ))

            self.payment_processing_state = None
        else:
            # Weird! Why was I called if there is nothing to do
            return
        self.save()

    @classmethod
    def process_all_pending_payments(cls):
        """Cron method authorizes waiting payments.
        """
        User = Pool().get('res.user')

        user = User(Transaction().user)
        if not (Transaction().context.get('company') or user.company):
            # Processing payments without user's company and company in
            # context is not possible at all. Skip the execution.
            return

        sales = cls.search([
            ('payment_processing_state', '!=', None)
        ])

        for sale in sales:
            sale.process_pending_payments()

    @classmethod
    def copy(cls, records, default=None):
        """
        Duplicating records
        """
        if default is None:
            default = {}

        default['payment_processing_state'] = None
        default['payments'] = []

        return super(Sale, cls).copy(records, default)


class PaymentTransaction:
    "Gateway Transaction"
    __name__ = 'payment_gateway.transaction'

    sale_payment = fields.Many2One(
        'sale.payment', 'Sale Payment', ondelete='RESTRICT', select=True,
    )

    def get_shipping_address(self, name):
        return self.sale_payment and self.sale_payment.sale and \
            self.sale_payment.sale.shipment_address.id


class AddSalePaymentView(BaseCreditCardViewMixin, ModelView):
    """
    View for adding Sale Payments
    """
    __name__ = 'sale.payment.add_view'

    sale = fields.Many2One(
        'sale.sale', 'Sale', required=True, readonly=True
    )

    party = fields.Many2One('party.party', 'Party', readonly=True)
    gateway = fields.Many2One(
        'payment_gateway.gateway', 'Gateway', required=True,
        domain=[('users', '=', Eval('user'))],
        depends=['user']
    )
    currency_digits = fields.Function(
        fields.Integer('Currency Digits'),
        'get_currency_digits'
    )
    method = fields.Function(
        fields.Char('Payment Gateway Method'), 'get_method'
    )
    use_existing_card = fields.Boolean(
        'Use existing Card?', states={
            'invisible': Eval('method') != 'credit_card'
        }, depends=['method']
    )
    payment_profile = fields.Many2One(
        'party.payment_profile', 'Payment Profile',
        domain=[
            ('party', '=', Eval('party')),
            ('gateway', '=', Eval('gateway')),
        ],
        states={
            'required': And(
                Eval('method') == 'credit_card', Bool(Eval('use_existing_card'))
            ),
            'invisible': ~Bool(Eval('use_existing_card'))
        }, depends=['method', 'use_existing_card', 'party', 'gateway']
    )
    amount = fields.Numeric(
        'Amount', digits=(16, Eval('currency_digits', 2)),
        required=True, depends=['currency_digits'],
    )
    reference = fields.Char(
        'Reference', states={
            'invisible': Not(Eval('method') == 'manual'),
        }
    )
    user = fields.Many2One(
        "res.user", "Tryton User", readonly=True
    )

    company = fields.Many2One(
        'company.company', 'Company', readonly=True, required=True,
        domain=[
            ('id', If(Eval('context', {}).contains('company'), '=', '!='),
                Eval('context', {}).get('company', -1)),
        ],
    )
    credit_account = fields.Many2One(
        'account.account', 'Credit Account', required=True
    )

    @classmethod
    def __setup__(cls):
        super(AddSalePaymentView, cls).__setup__()

        INV = Or(
            Eval('method') == 'manual',
            And(
                Eval('method') == 'credit_card',
                Bool(Eval('use_existing_card'))
            )
        )
        STATE1 = {
            'required': And(
                ~Bool(Eval('use_existing_card')),
                Eval('method') == 'credit_card'
            ),
            'invisible': INV
        }
        DEPENDS = ['use_existing_card', 'method']

        cls.owner.states.update(STATE1)
        cls.owner.depends.extend(DEPENDS)
        cls.number.states.update(STATE1)
        cls.number.depends.extend(DEPENDS)
        cls.expiry_month.states.update(STATE1)
        cls.expiry_month.depends.extend(DEPENDS)
        cls.expiry_year.states.update(STATE1)
        cls.expiry_year.depends.extend(DEPENDS)
        cls.csc.states.update(STATE1)
        cls.csc.depends.extend(DEPENDS)
        cls.swipe_data.states = {'invisible': INV}
        cls.swipe_data.depends = ['method']

        cls.credit_account.domain = [
            ('company', '=', Eval('company', -1)),
            ('kind', 'in', cls._credit_account_domain())
        ]
        cls.credit_account.depends = ['company']

    def get_currency_digits(self, name):
        return self.sale.currency_digits if self.sale else 2

    def get_method(self, name=None):
        """
        Return the method based on the gateway
        """
        return self.gateway.method

    @fields.depends('gateway')
    def on_change_gateway(self):
        if self.gateway:
            self.method = self.gateway.method or None

    @classmethod
    def _credit_account_domain(cls):
        """
        Return a list of account kind
        """
        return ['receivable']


class AddSalePayment(Wizard):
    """
    Wizard to add a Sale Payment
    """
    __name__ = 'sale.payment.add'

    start_state = 'payment_info'

    payment_info = StateView(
        'sale.payment.add_view',
        'sale_payment_gateway.sale_payment_add_view_form',
        [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('Add', 'add', 'tryton-ok', default=True)
        ]
    )
    add = StateTransition()

    def default_payment_info(self, fields=None):
        Sale = Pool().get('sale.sale')

        sale = Sale(Transaction().context.get('active_id'))

        res = {
            'sale': sale.id,
            'company': sale.company.id,
            'party': sale.party.id,
            'credit_account': sale.party.account_receivable.id,
            'owner': sale.party.name,
            'currency_digits': sale.currency_digits,
            'amount': sale.total_amount - sale.payment_total,
            'user': Transaction().user,
        }
        return res

    def create_sale_payment(self, profile=None):
        """
        Helper function to create new payment
        """
        SalePayment = Pool().get('sale.payment')

        payment_profile = profile
        if self.payment_info.method != 'credit_card':
            payment_profile = None

        return SalePayment(
            sale=Transaction().context.get('active_id'),
            credit_account=self.payment_info.credit_account,
            party=self.payment_info.party,
            gateway=self.payment_info.gateway,
            payment_profile=payment_profile,
            amount=self.payment_info.amount,
            reference=self.payment_info.reference or None,
        )

    def create_payment_profile(self):
        """
        Helper function to create payment profile
        """
        Sale = Pool().get('sale.sale')
        ProfileWizard = Pool().get(
            'party.party.payment_profile.add', type="wizard"
        )
        profile_wizard = ProfileWizard(
            ProfileWizard.create()[0]
        )
        profile_wizard.card_info.owner = self.payment_info.owner
        profile_wizard.card_info.number = self.payment_info.number
        profile_wizard.card_info.expiry_month = self.payment_info.expiry_month
        profile_wizard.card_info.expiry_year = self.payment_info.expiry_year
        profile_wizard.card_info.csc = self.payment_info.csc or ''
        profile_wizard.card_info.gateway = self.payment_info.gateway
        profile_wizard.card_info.provider = self.payment_info.gateway.provider
        profile_wizard.card_info.party = self.payment_info.party

        billing_address = Sale(
            Transaction().context.get('active_id')
        ).invoice_address
        if not billing_address:
            # If no billing address fallback to party's invoice address
            try:
                billing_address = self.payment_info.party.address_get(
                    type='invoice'
                )
            except AttributeError:
                # account_invoice module is not installed
                pass

        profile_wizard.card_info.address = billing_address

        with Transaction().set_context(return_profile=True):
            profile = profile_wizard.transition_add()
        return profile

    def transition_add(self):
        """
        Creates a new payment and ends the wizard
        """
        profile = self.payment_info.payment_profile
        if self.payment_info.method == 'credit_card' and (
            not self.payment_info.use_existing_card
        ):
            profile = self.create_payment_profile()

        payment = self.create_sale_payment(profile=profile)
        payment.save()
        return 'end'
