import base64
import json

import pytest

rsa = pytest.importorskip("rsa")
pytest.importorskip("alipay.aop.api")
from alipay.aop.api.util.SignatureUtils import get_sign_content
from Crypto.PublicKey import RSA

from token_mentor.tools.alipay import AlipayGateway, AlipaySettings, amount_fen


@pytest.fixture
def signed_gateway(tmp_path):
    key = RSA.generate(2048)
    private = tmp_path / "app.pem"
    public = tmp_path / "alipay.pem"
    private.write_bytes(key.export_key(pkcs=8))
    public.write_bytes(key.public_key().export_key())
    settings = AlipaySettings("app-id", "seller-id", str(private), str(public),
                              "https://merchant.example/v1/payments/alipay/notify", True)
    return AlipayGateway(settings), rsa.PrivateKey.load_pkcs1(key.export_key(pkcs=1))


def sign(params, private, algorithm="SHA-256"):
    unsigned = {key: value for key, value in params.items() if key not in {"sign", "sign_type"} and value != ""}
    return base64.b64encode(rsa.sign(get_sign_content(unsigned).encode(), private, algorithm)).decode()


def test_real_rsa2_notification_verification_and_tampering(signed_gateway):
    gateway, private = signed_gateway
    params = {"app_id": "app-id", "seller_id": "seller-id", "out_trade_no": "order",
              "trade_no": "trade", "trade_status": "TRADE_SUCCESS", "total_amount": "10.00", "sign_type": "RSA2"}
    params["sign"] = sign(params, private)
    assert gateway.verify_notification(params)
    assert not gateway.verify_notification({**params, "total_amount": "0.01"})
    assert not gateway.verify_notification({**params, "app_id": "other"})
    assert not gateway.verify_notification({**params, "seller_id": "other"})
    assert not gateway.verify_notification({**params, "sign_type": "RSA"})
    assert not gateway.verify_notification({**params, "sign": sign(params, private, "SHA-1")})


def test_official_sdk_signed_precreate_response_and_unsigned_rejection(signed_gateway, monkeypatch):
    gateway, private = signed_gateway
    import alipay.aop.api.DefaultAlipayClient as sdk_module

    order = {"order_id": "test-order", "amount_fen": 1001, "credits": 10}
    result = {"code": "10000", "out_trade_no": "test-order", "qr_code": "https://qr.alipay.com/example"}
    body = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    signature = base64.b64encode(rsa.sign(body.encode(), private, "SHA-256")).decode()
    response = ('{"alipay_trade_precreate_response":' + body + ',"sign":"' + signature + '"}').encode()
    captured = []

    def fake_post(*args, **kwargs):
        captured.append((args, kwargs))
        return response

    monkeypatch.setattr(sdk_module, "do_post", fake_post)
    assert gateway.precreate(order) == result["qr_code"]
    assert captured
    # The request is really signed and the SDK is aimed at its sandbox gateway.
    assert "openapi.alipaydev.com" in str(captured[0])
    assert json.loads(captured[0][0][3]["biz_content"])["total_amount"] == "10.01"
    monkeypatch.setattr(sdk_module, "do_post", lambda *a, **kw: b'{"alipay_trade_precreate_response":{"code":"10000"}}')
    from token_mentor.tools.alipay import PaymentUnavailable
    with pytest.raises(PaymentUnavailable):
        gateway.precreate(order)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0", "1.001", "not-money"])
def test_invalid_amount(value):
    with pytest.raises(ValueError):
        amount_fen(value)
