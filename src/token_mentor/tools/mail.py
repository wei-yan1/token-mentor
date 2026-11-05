"""QQ mailbox verification-code delivery using standard-library SMTP."""
import os
import smtplib
from email.message import EmailMessage


class MailUnavailable(RuntimeError):
    pass


class SmtpMailer:
    def send_code(self, recipient: str, code: str, purpose: str):
        username = os.getenv("TOKEN_MENTOR_SMTP_USERNAME", "")
        password = os.getenv("TOKEN_MENTOR_SMTP_PASSWORD", "")
        if not username or not password:
            raise MailUnavailable("邮箱发送服务尚未配置，请配置 QQ 邮箱 SMTP 账号和授权码")
        message = EmailMessage()
        message["Subject"] = "Token Mentor 邮箱认证验证码"
        message["From"] = username
        message["To"] = recipient
        action = "创建账户" if purpose == "register" else "登录账户"
        message.set_content(f"你正在{action}。\n\n验证码：{code}\n\n10 分钟内有效，仅可使用一次。\n如果这不是你的操作，请忽略此邮件。")
        try:
            with smtplib.SMTP_SSL(os.getenv("TOKEN_MENTOR_SMTP_HOST", "smtp.qq.com"),
                                  int(os.getenv("TOKEN_MENTOR_SMTP_PORT", "465")), timeout=15) as smtp:
                smtp.login(username, password)
                smtp.send_message(message)
        except Exception as exc:
            raise MailUnavailable("验证码发送失败，请稍后重试或检查邮箱 SMTP 配置") from exc
