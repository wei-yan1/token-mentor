"""Reserve a one-time challenge, deliver it, release failed deliveries."""
import secrets


class EmailAuthentication:
    def __init__(self, auth, mailer):
        self.auth = auth
        self.mailer = mailer

    def send_code(self, email, purpose):
        code = f"{secrets.randbelow(1_000_000):06d}"
        challenge = self.auth.issue_code(email, purpose, code)
        try:
            self.mailer.send_code(email, code, purpose)
        except Exception:
            self.auth.cancel_code(email, purpose, challenge)
            raise
