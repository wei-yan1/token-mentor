"""Real-browser acceptance flow against an isolated database and a fake payment gateway.

Run: uv run --extra api --extra dev --extra browser python tests/browser_web.py
Uses local Edge when available, otherwise install Chromium with playwright install chromium.
"""
import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import uvicorn
from playwright.sync_api import expect, sync_playwright

from token_mentor.api import create_app
from token_mentor.models import ModelQuote, utc_now
from token_mentor.state.storage import Catalog


def main():
    output = Path("reports/ui")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / "browser.sqlite3"
        catalog = Catalog(db)
        catalog.upsert_quotes([ModelQuote.from_payload({
            "provider": "Browser Test Fixture", "model": "fixture-model",
            "input_price_per_million": 1, "output_price_per_million": 2,
            "price_unit": "CNY/1M tokens", "source_url": "https://example.invalid/test-fixture",
            "observed_at": utc_now(), "capabilities": {"coding": .8, "summarization": .8},
        })])
        gateway = SimpleNamespace(
            precreate=lambda order: "https://qr.alipay.com/browser-test-" + order["order_id"],
            verify_notification=lambda params: params.get("sign") == "browser-test-only",
        )
        codes = {}
        app = create_app(str(db), payment_gateway=gateway, mailer=SimpleNamespace(send_code=lambda email,code,purpose: codes.update({(email,purpose):code})))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(.05)
            assert server.started, "Test server did not start"
            base = f"http://127.0.0.1:{port}"
            with sync_playwright() as playwright:
                edge = Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
                browser = playwright.chromium.launch(**({"executable_path": str(edge)} if edge.exists() else {}), headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base + "/login?mode=register")
                page.get_by_label("如何称呼你").fill("界面验收团队")
                page.get_by_label("账号", exact=True).fill("browser_test")
                page.get_by_label("QQ 邮箱", exact=True).fill("browser_test@qq.com")
                page.locator("#send-code").click()
                expect(page.locator("#code-status")).to_contain_text("已发送")
                page.get_by_label("邮箱验证码").fill(codes[("browser_test@qq.com","register")])
                page.get_by_label("密码", exact=True).fill("browser-test-password-123")
                page.get_by_role("button", name="认证邮箱并创建账户").click()
                page.wait_for_url("**/app")
                expect(page.locator("#account-name")).to_have_text("界面验收团队")
                expect(page.locator("#credit-count")).to_have_text("0")
                page.screenshot(path=str(output / "dashboard-empty.png"), full_page=True)

                page.locator('[data-view="projects"]').click()
                page.get_by_role("button", name="新建项目", exact=True).click()
                page.get_by_label("项目名称", exact=True).fill("知识库助手")
                page.locator("#project-submit").click()
                expect(page.locator("#secret-dialog")).to_be_visible()
                first_key = page.locator("#new-secret").inner_text()
                assert first_key.startswith("tm_")
                page.locator('[data-close="secret-dialog"]').click()
                expect(page.locator("#projects-grid")).to_contain_text("知识库助手")

                page.locator('[data-view="recommend"]').click()
                page.locator("#recommend-submit").click()
                expect(page.locator("#recommend-error")).to_contain_text("次数不足")

                page.locator('[data-view="billing"]').click()
                page.locator("#buy-pack").click()
                expect(page.locator("#payment-qr img")).to_be_visible()
                expect(page.locator("#payment-qr img")).not_to_have_js_property("naturalWidth", 0)
                page.screenshot(path=str(output / "payment-desktop.png"))
                order = page.request.get(base + "/v1/billing/orders").json()[0]
                notification = urlencode({"sign": "browser-test-only", "out_trade_no": order["order_id"],
                    "trade_no": "browser-transaction", "trade_status": "TRADE_SUCCESS",
                    "total_amount": f"{order['amount_fen']/100:.2f}"}).encode()
                request = Request(base + "/v1/payments/alipay/notify", data=notification,
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
                with urlopen(request, timeout=5) as response:
                    assert response.read() == b"success"
                page.locator("#check-payment").click()
                expect(page.locator("#payment-description")).to_contain_text("次数已到账")
                page.locator('[data-close="payment-dialog"]').click()

                page.locator('[data-view="recommend"]').click()
                page.get_by_label("通用问答", exact=True).uncheck()
                page.get_by_label("编码与代码分析", exact=True).check()
                page.locator("#recommend-submit").click()
                expect(page.locator("#report-panel")).to_be_visible()
                expect(page.locator("#report-content")).to_contain_text("fixture-model")
                assert page.request.get(base + "/v1/billing/balance").json()["credits"] == order["credits"] - 1
                with page.expect_download() as download:
                    page.locator("#download-report").click()
                assert download.value.suggested_filename.endswith(".md")
                download.value.save_as(output / "browser-fixture-report.md")
                page.locator("#feedback-note").fill("浏览器端到端验证")
                page.locator("#feedback-submit").click()
                expect(page.locator("#feedback-message")).to_contain_text("评价已记录")

                page.locator('[data-analysis="cloud"]').click()
                page.locator("#intent").fill("为客服对话生成简短中文摘要")
                page.locator("#content-consent").check()
                page.locator("#recommend-submit").click()
                expect(page.locator("#report-meta")).to_contain_text("本次成功调用计 1 次")
                expect(page.locator("#recommend-submit")).not_to_be_disabled()
                page.locator("#clear-memory").click()
                expect(page.locator("#toast")).to_contain_text("记忆已清除")

                page.locator('[data-view="keys"]').click()
                page.locator("#issue-key").click()
                with page.expect_response(lambda response: response.url.endswith("/v1/keys") and response.request.method == "POST") as issued:
                    page.locator("#key-submit").click()
                key = issued.value.json()
                expect(page.locator("#secret-dialog")).to_be_visible()
                page.locator('[data-close="secret-dialog"]').click()
                row = page.get_by_role("row").filter(has_text=key["key_id"][:12])
                row.get_by_role("button", name="撤销", exact=True).click()
                expect(row).to_contain_text("已撤销")
                assert page.request.get(base + "/v1/billing/balance", headers={"Authorization": f"Bearer {key['api_key']}"}).status == 401
                assert page.evaluate("Object.keys(localStorage).length") == 0

                for width in (320, 375, 414, 768, 1440):
                    page.set_viewport_size({"width": width, "height": 1000})
                    for view in ("overview", "recommend", "projects", "keys", "billing"):
                        page.goto(base + "/app#" + view)
                        expect(page.locator("#main")).to_have_attribute("aria-busy", "false")
                        page.locator(".view:visible").evaluate("element => Promise.all(element.getAnimations().map(animation => animation.finished))")
                        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), (width, view)
                        if width in (375,1440) and view in ("overview","recommend","billing"):
                            page.screenshot(path=str(output / f"{view}-{width}.png"), full_page=True)

                page.locator("#logout").click()
                page.wait_for_url("**/login")
                page.get_by_label("账号 / 邮箱").fill("browser_test")
                page.get_by_label("密码",exact=True).fill("browser-test-password-123")
                page.locator("#auth-submit").click()
                page.wait_for_url("**/app")
                expect(page.locator("#main")).to_have_attribute("aria-busy", "false")
                assert page.request.get(base + "/v1/auth/me").json()["can_manage"] is True
                assert page.request.post(base + "/v1/auth/key-login", data={"api_key":first_key},headers={"X-Requested-With":"TokenMentor"}).status == 404
                assert not errors, errors
                browser.close()
            print(json.dumps({"passed": True, "flows": ["register", "project", "zero-credit rejection", "payment QR + verified fixture callback", "paid recommendation", "Markdown download", "cloud consent + memory", "issue + revoke key", "logout + account password login"], "responsive_widths": [320,375,414,768,1440], "javascript_errors": errors}, ensure_ascii=False))
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
