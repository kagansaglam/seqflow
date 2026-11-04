import json
import logging
import urllib.error
from typing import ClassVar

import pytest

from seqflow.exceptions import NotificationError
from seqflow.notify import EmailChannel, Notifier, SlackChannel, notifier_from_env

URL = "https://hooks.example.test/services/T000/B000/SECRETSECRET"


class Recorder:
    def __init__(self, fail=False):
        self.msgs, self.fail = [], fail

    def send(self, level, subject, body):
        if self.fail:
            raise RuntimeError("channel down")
        self.msgs.append((level, subject, body))


def test_levels_decide_who_is_told():
    log, alert = Recorder(), Recorder()
    n = Notifier([log], [alert])
    n.notify("info", "a")
    n.notify("warning", "b")
    n.notify("error", "c", "details")
    assert [m[1] for m in log.msgs] == ["a", "b", "c"]
    assert alert.msgs == [("error", "c", "details")]


def test_notify_all_reaches_alert_channels_at_any_level():
    log, alert = Recorder(), Recorder()
    Notifier([log], [alert]).notify_all("info", "digest", "body")
    assert log.msgs == alert.msgs == [("info", "digest", "body")]


def test_a_failing_alert_channel_never_stops_the_others(caplog):
    good = Recorder()
    n = Notifier([Recorder()], [Recorder(fail=True), good])
    with caplog.at_level(logging.ERROR, logger="seqflow"):
        n.notify("error", "x")
    assert good.msgs == [("error", "x", "")]
    assert "alert channel" in caplog.text


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"ok"


def http_error(code):
    return urllib.error.HTTPError(URL, code, "boom", {}, None)


def make_opener(*outcomes):
    """Each call pops the next outcome: an exception to raise, or None for success."""
    seen = []

    def opener(request, timeout):
        seen.append((request, timeout))
        outcome = outcomes[min(len(seen), len(outcomes)) - 1]
        if outcome is not None:
            raise outcome
        return FakeResponse()

    opener.seen = seen
    return opener


def test_slack_posts_json_to_the_webhook():
    opener = make_opener(None)
    SlackChannel(URL, opener=opener).send("error", "run X failed", "demux exploded")
    request, timeout = opener.seen[0]
    assert request.full_url == URL and request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    text = json.loads(request.data)["text"]
    assert "[ERROR] run X failed" in text and "demux exploded" in text
    assert timeout == 10.0


def test_slack_retries_network_errors_and_server_errors():
    slept = []
    opener = make_opener(urllib.error.URLError("dns"), http_error(503), None)
    SlackChannel(URL, opener=opener, sleep=slept.append).send("error", "x", "")
    assert len(opener.seen) == 3 and len(slept) == 2


def test_slack_does_not_retry_client_errors():
    opener = make_opener(http_error(403))
    with pytest.raises(NotificationError, match="HTTP 403") as info:
        SlackChannel(URL, opener=opener, sleep=lambda _: None).send("error", "x", "")
    assert len(opener.seen) == 1
    assert "SECRET" not in str(info.value)


def test_slack_gives_up_after_the_configured_attempts():
    opener = make_opener(urllib.error.URLError("down"))
    with pytest.raises(urllib.error.URLError):
        SlackChannel(URL, opener=opener, attempts=2, sleep=lambda _: None).send("error", "x", "")
    assert len(opener.seen) == 2


def test_slack_never_shows_the_webhook_url():
    assert "SECRET" not in repr(SlackChannel(URL))


class FakeSMTP:
    instances: ClassVar[list] = []
    fail_first = 0

    def __init__(self, host, port, timeout):
        if FakeSMTP.fail_first:
            FakeSMTP.fail_first -= 1
            raise ConnectionRefusedError("smtp down")
        self.host, self.port, self.timeout = host, port, timeout
        self.actions = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.actions.append("starttls")

    def login(self, user, password):
        self.actions.append(("login", user, password))

    def send_message(self, msg):
        self.actions.append(("send", msg["Subject"], msg["To"], msg.get_content().strip()))


@pytest.fixture(autouse=True)
def reset_smtp():
    FakeSMTP.instances, FakeSMTP.fail_first = [], 0


def test_email_sends_with_tls_and_login():
    ch = EmailChannel("mail.test", "seqflow@lab.test", ["a@lab.test", "b@lab.test"], 587,
                      starttls=True, username="u", password="pw", smtp_factory=FakeSMTP)
    ch.send("error", "run X failed", "details")
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == ("mail.test", 587)
    assert smtp.actions == ["starttls", ("login", "u", "pw"),
                            ("send", "[seqflow:error] run X failed", "a@lab.test, b@lab.test",
                             "details")]


def test_email_retries_connection_failures():
    FakeSMTP.fail_first = 2
    slept = []
    EmailChannel("mail.test", "f@x", ["t@x"], smtp_factory=FakeSMTP, sleep=slept.append).send(
        "error", "s", "b")
    assert len(slept) == 2 and len(FakeSMTP.instances) == 1


def test_email_repr_hides_the_password():
    ch = EmailChannel("mail.test", "f@x", ["t@x"], password="hunter2")
    assert "hunter2" not in repr(ch)


def test_notifier_from_env_without_configuration_has_no_alert_channels():
    assert notifier_from_env({}).alert_channels == []


def test_notifier_from_env_builds_slack_and_email():
    n = notifier_from_env({
        "SEQFLOW_SLACK_WEBHOOK": URL,
        "SEQFLOW_SMTP_HOST": "mail.test", "SEQFLOW_SMTP_PORT": "587",
        "SEQFLOW_SMTP_STARTTLS": "1", "SEQFLOW_SMTP_USER": "u", "SEQFLOW_SMTP_PASSWORD": "pw",
        "SEQFLOW_MAIL_FROM": "seqflow@lab.test", "SEQFLOW_MAIL_TO": "a@lab.test, b@lab.test ,",
    })
    slack, mail = n.alert_channels
    assert isinstance(slack, SlackChannel) and isinstance(mail, EmailChannel)
    assert (mail.host, mail.port, mail.starttls) == ("mail.test", 587, True)
    assert mail.recipients == ["a@lab.test", "b@lab.test"]
    assert (mail.username, mail.password) == ("u", "pw")


def test_email_needs_both_host_and_recipients(caplog):
    with caplog.at_level(logging.WARNING, logger="seqflow"):
        n = notifier_from_env({"SEQFLOW_SMTP_HOST": "mail.test"})
    assert n.alert_channels == [] and "e-mail disabled" in caplog.text
    assert notifier_from_env({"SEQFLOW_MAIL_TO": "a@b"}).alert_channels == []
