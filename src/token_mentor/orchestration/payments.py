"""Verified gateway results become atomic, idempotent credit grants."""
from token_mentor.state.billing import BillingError, BillingStore
from token_mentor.tools.alipay import amount_fen


class PaymentService:
    def __init__(self, store: BillingStore, gateway):
        self.store = store
        self.gateway = gateway

    def checkout(self, principal, order_id):
        self.store.require_account_key(principal)
        order = self.store.order(principal, order_id)
        if order["status"] == "paid":
            return order
        if not order["qr_code"]:
            self.store.set_qr(order_id, self.gateway.precreate(order))
        return self.store.order(principal, order_id)

    def notification(self, params):
        if not self.gateway.verify_notification(params):
            raise BillingError("支付宝通知验签或商户校验失败", 400)
        if params.get("trade_status") in {"TRADE_SUCCESS", "TRADE_FINISHED"}:
            self.store.settle_order(params.get("out_trade_no", ""), amount_fen(params.get("total_amount", "")),
                                    params.get("trade_no", ""))

    def reconcile(self, principal, order_id):
        self.store.require_account_key(principal)
        order = self.store.order(principal, order_id)
        if order["status"] == "paid":
            return order
        result = self.gateway.query(order)
        # trade.query is signed for our app and normally omits seller_id.
        if result.get("out_trade_no") != order_id:
            raise BillingError("支付宝查询返回的订单不匹配", 400)
        if result.get("seller_id") and result["seller_id"] != self.gateway.settings.seller_id:
            raise BillingError("支付宝查询返回的商户不匹配", 400)
        if result.get("trade_status") in {"TRADE_SUCCESS", "TRADE_FINISHED"}:
            self.store.settle_order(order_id, amount_fen(result.get("total_amount", "")), result.get("trade_no", ""))
        return self.store.order(principal, order_id)
