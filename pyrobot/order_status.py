from pyrobot.trades import Trade

class OrderStatus():

    def __init__(self, trade_obj: Trade) -> None:

        self.trade_obj = trade_obj
        self.order_status = self.trade_obj.order_status

    @property
    def is_cancelled(self) -> bool:
        """Returns True if the order status is CANCELLED."""
        self.trade_obj._update_order_status()
        return self.order_status == 'CANCELLED'

    @property
    def is_filled(self) -> bool:
        """Returns True if the order status is FILLED."""
        self.trade_obj._update_order_status()
        return self.order_status == 'FILLED'

    @property
    def is_rejected(self) -> bool:
        """Returns True if the order status is REJECTED."""
        self.trade_obj._update_order_status()
        return self.order_status == 'REJECTED'

    @property
    def is_expired(self) -> bool:
        """Returns True if the order status is EXPIRED."""
        self.trade_obj._update_order_status()
        return self.order_status == 'EXPIRED'

    @property
    def is_replaced(self) -> bool:
        """Returns True if the order status is REPLACED."""
        self.trade_obj._update_order_status()
        return self.order_status == 'REPLACED'

    @property
    def is_working(self) -> bool:
        """Returns True if the order status is WORKING."""
        self.trade_obj._update_order_status()
        return self.order_status == 'WORKING'

    @property
    def is_pending_activation(self) -> bool:
        """Returns True if the order status is PENDING_ACTIVATION."""
        self.trade_obj._update_order_status()
        return self.order_status == 'PENDING_ACTIVATION'

    @property
    def is_pending_cancel(self) -> bool:
        """Returns True if the order status is PENDING_CANCEL."""
        self.trade_obj._update_order_status()
        return self.order_status == 'PENDING_CANCEL'

    @property
    def is_pending_replace(self) -> bool:
        """Returns True if the order status is PENDING_REPLACE."""
        self.trade_obj._update_order_status()
        return self.order_status == 'PENDING_REPLACE'

    @property
    def is_queued(self) -> bool:
        """Returns True if the order status is QUEUED."""
        self.trade_obj._update_order_status()
        return self.order_status == 'QUEUED'

    @property
    def is_accepted(self) -> bool:
        """Returns True if the order status is ACCEPTED."""
        self.trade_obj._update_order_status()
        return self.order_status == 'ACCEPTED'

    @property
    def is_awaiting_parent_order(self) -> bool:
        """Returns True if the order status is AWAITING_PARENT_ORDER."""
        self.trade_obj._update_order_status()
        return self.order_status == 'AWAITING_PARENT_ORDER'

    @property
    def is_awaiting_condition(self) -> bool:
        """Returns True if the order status is AWAITING_CONDITION."""
        self.trade_obj._update_order_status()
        return self.order_status == 'AWAITING_CONDITION'
