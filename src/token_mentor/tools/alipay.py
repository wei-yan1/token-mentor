"""Alipay face-to-face payments via the official SDK; no payment state here."""
import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


class PaymentUnavailable(RuntimeError):
    pass


def amount_fen(value: str) -> int:
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount <= 0 or amount * 100 != (amount * 100).to_integral_value():
            raise ValueError("支付宝金额必须为正数且最多两位小数")
        return int(amount * 100)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("无效金额") from exc


@dataclass(frozen=True)
class AlipaySettings:
    app_id: str = ""
    seller_id: str = ""
    private_key_file: str = ""
    public_key_file: str = ""
    notify_url: str = ""
    sandbox: bool = True

    @classmethod
    def from_env(cls):
        mode = os.getenv("ALIPAY_SANDBOX", "true").lower()
        if mode not in {"true", "false"}:
            raise ValueError("ALIPAY_SANDBOX 只能设置为 true 或 false")
        return cls(os.getenv("ALIPAY_APP_ID", ""), os.getenv("ALIPAY_SELLER_ID", ""),
                   os.getenv("ALIPAY_PRIVATE_KEY_FILE", ""), os.getenv("ALIPAY_PUBLIC_KEY_FILE", ""),
                   os.getenv("ALIPAY_NOTIFY_URL", ""), mode == "true")


class AlipayGateway:
    def __init__(self, settings: AlipaySettings):
        self.settings = settings

    def _public_key(self):
        try:
            return Path(self.settings.public_key_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PaymentUnavailable("支付宝公钥文件不可用") from exc

    def _client(self):
        settings = self.settings
        if not all((settings.app_id, settings.seller_id, settings.private_key_file,
                    settings.public_key_file, settings.notify_url.startswith("https://"))):
            raise PaymentUnavailable("请配置支付宝应用、商户、密钥文件和 HTTPS 回调地址")
        try:
            from alipay.aop.api.AlipayClientConfig import AlipayClientConfig
            from alipay.aop.api.DefaultAlipayClient import DefaultAlipayClient
            from Crypto.PublicKey import RSA
            config = AlipayClientConfig(sandbox_debug=settings.sandbox)
            config.app_id = settings.app_id
            # Normalize both PKCS#8 and PKCS#1 PEM files for the official SDK.
            config.app_private_key = RSA.import_key(Path(settings.private_key_file).read_bytes()).export_key(pkcs=1).decode()
            config.alipay_public_key = self._public_key()
            config.sign_type = "RSA2"
            config.timeout = 15
            return DefaultAlipayClient(config)
        except (ImportError, OSError, ValueError) as exc:
            raise PaymentUnavailable("支付宝 SDK 或密钥不可用；安装 .[payments] 并配置密钥文件") from exc

    def _execute(self, request):
        try:
            result = json.loads(self._client().execute(request))  # SDK verifies the response signature.
        except PaymentUnavailable:
            raise
        except Exception as exc:
            raise PaymentUnavailable("支付宝网关请求失败，可稍后对同一订单重试") from exc
        if result.get("code") != "10000":
            raise PaymentUnavailable("支付宝未接受本次订单操作，请检查应用产品权限及订单状态")
        return result

    def precreate(self, order: dict) -> str:
        try:
            from alipay.aop.api.request.AlipayTradePrecreateRequest import (
                AlipayTradePrecreateRequest,
            )
        except ImportError as exc:
            raise PaymentUnavailable("请安装 .[payments]") from exc
        request = AlipayTradePrecreateRequest()
        request.notify_url = self.settings.notify_url
        request.biz_content = {"out_trade_no": order["order_id"],
                               "total_amount": f"{order['amount_fen'] // 100}.{order['amount_fen'] % 100:02d}",
                               "subject": f"Token Mentor 推荐次数包 {order['credits']} 次",
                               "timeout_express": "30m"}
        result = self._execute(request)
        if result.get("out_trade_no") != order["order_id"] or not result.get("qr_code"):
            raise PaymentUnavailable("支付宝返回的订单信息不完整")
        return result["qr_code"]

    def query(self, order: dict) -> dict:
        try:
            from alipay.aop.api.request.AlipayTradeQueryRequest import (
                AlipayTradeQueryRequest,
            )
        except ImportError as exc:
            raise PaymentUnavailable("请安装 .[payments]") from exc
        request = AlipayTradeQueryRequest()
        request.biz_content = {"out_trade_no": order["order_id"]}
        result = self._execute(request)
        if result.get("out_trade_no") != order["order_id"]:
            raise ValueError("支付宝返回的订单号不匹配")
        return result

    def verify_notification(self, params: dict[str, str]) -> bool:
        settings = self.settings
        if not settings.app_id or not settings.seller_id or params.get("sign_type") != "RSA2":
            return False
        if params.get("app_id") != settings.app_id or params.get("seller_id") != settings.seller_id:
            return False
        try:
            import base64

            import rsa
            from alipay.aop.api.util.SignatureUtils import (
                fill_public_key_marker,
                get_sign_content,
            )
            unsigned = {key: value for key, value in params.items() if key not in {"sign", "sign_type"} and value != ""}
            content = get_sign_content(unsigned).encode("utf-8")
            public = rsa.PublicKey.load_pkcs1_openssl_pem(fill_public_key_marker(self._public_key()))
            return rsa.verify(content, base64.b64decode(params.get("sign", ""), validate=True), public) == "SHA-256"
        except Exception:  # noqa: BLE001 -- Any verification error must fail closed.
            return False
