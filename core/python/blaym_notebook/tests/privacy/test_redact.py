"""Secret hiding: patterns, properties, the true/false-positive corpus, events.

Read the CORPUS first: it states, for each ordinary-looking text, whether it is hidden or
kept, and names the cases where a false positive or false negative is accepted.
"""

from __future__ import annotations

import json
import string
import time

import pytest
from hypothesis import assume, given, settings, strategies as st

from blaym_notebook.history import Rebuilder
from blaym_notebook.privacy import MARKER_RE, redact, redact_event
from blaym_notebook.tests.history.conftest import DEEP, MAX_EVENTS
from blaym_notebook.tests.history.generate import histories
from blaym_notebook.tests.history.scenario import EXPLORATION, Scenario, output_item
from blaym_notebook.tests.history.test_generator import _positions
from blaym_notebook_types import BlaymEvent

PROP = settings(max_examples=500 if DEEP else 200)

# --- the corpus -----------------------------------------------------------------------------
# (text, expected): expected None means "kept exactly"; a string is the exact output.
# Entries named KNOWN_FP / KNOWN_FN are accepted limits of a pattern-based filter.

KEPT = [
    "I forgot my password again, please reset it.",
    "The password field is required and must be 8 characters long.",
    "sha256: 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "commit 3b18e512dba79e4c8300dd08aeb37f8e728b8dad fixed the bug",
    "id = 123e4567-e89b-12d3-a456-426614174000",
    "iVBORw0KGgoAAAANSUhEUgAAAAUAAAAFCAYAAACNbyblAAAAHElEQVQI12P4//8/w38GIAXDIBKE0DHxgljNBAAO9TXL0Y4OHwAAAABJRU5ErkJggg==",
    "a_very_long_identifier_name_that_goes_on_and_on_and_on_for_a_while_without_end = 1",
    "password = getpass.getpass()",
    "token = tokenizer.encode(x)",
    "max_tokens=512",
    "n_tokens = 10",
    "secret_santa",
    "secret_santa = ['ann', 'bob']",
    "api_key = os.environ['KEY']",
    'api_key = os.environ["OPENAI_API_KEY"]',
    "api_key = os.getenv('KEY')",
    "password = my_password",
    "def connect(password=pw, token=tok):",
    "password = None",
    "token: null",
    'api_key = ""',
    "api_key = '<your-key>'",
    'password = "xxxx"',
    'secret = "..."',
    "TOKEN=",
    "password: ${DB_PASSWORD}",
    "token_count = 42",
    "tokens = tokenizer(text)",
    "num_tokens: 128",
    "password: str",
    "use_token = True",
    "print('Enter your password:')",
    "see https://example.com/docs/passwords for details",
    "https://user@example.com/path",
    "git clone https://github.com/org/repo.git",
    "ghp is short for something",
    "my_sk-learn_model",
    "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
    "password_prompt = 'Enter password:'",
    "Authorization: Bearer ${TOKEN}",
    "token = '<token>'",
    "eyJ is how base64 JSON starts",
]

# (text, exact output)
HIDDEN = [
    ('password = "hunter2x9"', 'password = "[hidden: password]"'),
    ("PASSWORD=hunter2x9", "PASSWORD=[hidden: password]"),
    ("export API_KEY=abc123def", "export API_KEY=[hidden: api key]"),
    ("%env OPENAI_TOKEN=abc123def", "%env OPENAI_TOKEN=[hidden: token]"),
    ("password: hunter2x9", "password: [hidden: password]"),
    ("  - password: hunter2x9", "  - password: [hidden: password]"),
    ('{"api_key": "abc123def"}', '{"api_key": "[hidden: api key]"}'),
    ("{'client_secret': 'abc123def'}", "{'client_secret': '[hidden: secret]'}"),
    ('os.environ["MY_TOKEN"] = "abc123def"', 'os.environ["MY_TOKEN"] = "[hidden: token]"'),
    ('password: str = "abc123def"', 'password: str = "[hidden: password]"'),
    ("password = hunter2x9", "password = [hidden: password]"),  # spaced, has a digit: a literal
    ("DB_URL=postgres://admin:s3cr3tPw@db.local:5432/app", "DB_URL=postgres://admin:[hidden: connection string password]@db.local:5432/app"),
    ("Authorization: Bearer abc.def-123", "Authorization: Bearer [hidden: token]"),
    ('headers = {"Authorization": "Basic dXNlcjpwYXNz"}', 'headers = {"Authorization": "Basic [hidden: token]"}'),
    ("curl --password=hunter2x9 host", "curl --password=[hidden: password] host"),
    ("key AKIAIOSFODNN7EXAMPLE end", "key [hidden: api key] end"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----\nafter", "[hidden: private key]\nafter"),
    ("-----BEGIN PRIVATE KEY-----\nMIIabc no end", "[hidden: private key]"),
    ("PASSWORD=hunter2x9 # the db", "PASSWORD=[hidden: password] # the db"),
    ("aws_secret_access_key = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'", "aws_secret_access_key = '[hidden: secret]'"),
]

# Accepted limits, kept as named data: each says what the filter does and why we accept it.
KNOWN_FP = {
    # Hidden although harmless: any quoted value after a keyword name, a literal bearer word.
    "quoted_token_type": ('"token": "bearer"', '"token": "[hidden: token]"'),
    "pwd_is_a_directory": ("PWD=/home/ann/work", "PWD=[hidden: password]"),
    "kwarg_alone_on_a_line": ("connect(\n    password=pw\n)", "connect(\n    password=[hidden: password]\n)"),
}
KNOWN_FN = {
    # Not hidden although secret: nothing in the text says it is one.
    "prose": "my password is hunter2",
    "bare_high_entropy": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "ini_variable_like": "password = hunter",  # spaced, no digit: looks like a variable
    "glued_key_format": "xAKIAIOSFODNN7EXAMPLE",  # key formats need a word boundary
    "secret_in_split_text": "my pass\nword is hun\nter2",
}


def test_corpus_hidden_and_kept():
    for text, want in HIDDEN:
        out, n = redact(text)
        assert out == want and n >= 1, text
        assert redact(out) == (out, 0), text  # idempotent
    for text in KEPT:
        assert redact(text) == (text, 0), text


def test_known_false_positives_and_negatives_stay_documented():
    for text, want in KNOWN_FP.values():
        assert redact(text)[0] == want
    for text in KNOWN_FN.values():
        assert redact(text) == (text, 0)


# --- planted secrets (property 1) ---------------------------------------------------------------

_ALNUM = string.ascii_letters + string.digits


def _s(alphabet, n):
    return st.text(alphabet=alphabet, min_size=n, max_size=n)


@st.composite
def value(draw):
    """A password-like value that is not a placeholder: starts with a letter other than x/y,
    holds a digit, no quotes, backslash, space or `#`."""
    first = draw(st.sampled_from([c for c in string.ascii_letters if c not in "xyXY"]))
    mid = draw(st.text(alphabet=_ALNUM + "!@%^&*+/~", min_size=7, max_size=20))
    return first + mid + draw(st.sampled_from(string.digits))


@st.composite
def key_format(draw):
    kind = draw(st.sampled_from(
        ["aws", "google", "ghp", "ghpat", "slack", "stripe", "openai", "anthropic", "jwt"]))
    if kind == "aws":
        return draw(st.sampled_from(["AKIA", "ASIA"])) + draw(_s(string.ascii_uppercase + string.digits, 16))
    if kind == "google":
        return "AIza" + draw(_s(_ALNUM + "_-", 35))
    if kind == "ghp":
        return draw(st.sampled_from(["ghp", "gho", "ghu", "ghs", "ghr"])) + "_" + draw(_s(_ALNUM, 36))
    if kind == "ghpat":
        return "github_pat_" + draw(_s(_ALNUM + "_", 40))
    if kind == "slack":
        return "xox" + draw(st.sampled_from("baprs")) + "-" + draw(_s(string.digits, 12)) + "-" + draw(_s(_ALNUM, 20))
    if kind == "stripe":
        return draw(st.sampled_from(["sk_live_", "sk_test_", "rk_live_"])) + draw(_s(_ALNUM, 24))
    if kind == "openai":
        return "sk-" + draw(_s(_ALNUM, 40))
    if kind == "anthropic":
        return "sk-ant-api03-" + draw(_s(_ALNUM + "_-", 50))
    return "eyJ" + draw(_s(_ALNUM + "_-", 20)) + "." + draw(_s(_ALNUM + "_-", 30)) + "." + draw(_s(_ALNUM + "_-", 30))


NAMES = ["password", "passwd", "pwd", "secret", "token", "api_key", "apikey", "access_key",
         "private_key", "client_secret", "DB_PASSWORD", "GITHUB_TOKEN", "OPENAI_API_KEY",
         "aws_secret_access_key", "AUTH_TOKEN", "slack-token"]


@st.composite
def planted(draw):
    """(text containing a secret, the secret string that must vanish)."""
    kind = draw(st.sampled_from(["format", "assign", "bearer", "basic", "pem", "url", "flag"]))
    sep = draw(st.sampled_from(["\n", " ", "\n\n", "; "]))
    if kind == "format":
        secret = draw(key_format())
        line = draw(st.sampled_from(["{}", "key is {}", 'k = "{}"', "see: {}.", "<{}>", "({})", "'{}'", "x={}", "[{}]"])).format(secret)
        return line, secret, sep
    if kind == "bearer":
        secret = draw(_s(_ALNUM + "._~+/-", 30)).strip("-")
        assume(len(secret) >= 20)
        return f"Authorization: Bearer {secret}", secret, sep
    if kind == "basic":
        secret = draw(_s(_ALNUM + "+/=", 24))
        return draw(st.sampled_from(["Authorization: Basic {}", '"Authorization": "Basic {}"', "authorization = 'basic {}'"])).format(secret), secret, sep
    if kind == "pem":
        body = "\n".join(draw(st.lists(_s(_ALNUM + "+/", 64), min_size=1, max_size=4)))
        flavour = draw(st.sampled_from(["", "RSA ", "EC ", "OPENSSH ", "ENCRYPTED "]))
        end = draw(st.booleans())
        text = f"-----BEGIN {flavour}PRIVATE KEY-----\n{body}" + (f"\n-----END {flavour}PRIVATE KEY-----" if end else "")
        return text, body.split("\n")[0], "\n"
    if kind == "url":
        pw = draw(_s(_ALNUM + "!$%^&*+-._~", 12))
        assume(not pw.startswith(("$", "%")))
        scheme = draw(st.sampled_from(["postgres", "mysql", "mongodb+srv", "redis", "https", "amqp"]))
        user = draw(_s(string.ascii_lowercase, 5))
        return f"{draw(st.sampled_from(['', 'url = ', 'DB=', 'uri: ']))}{scheme}://{user}:{pw}@host.example:5432/db", pw, sep
    name = draw(st.sampled_from(NAMES))
    v = draw(value())
    if kind == "flag":
        return f"run --{name.lower().replace('_', '-')}={v} --verbose", v, sep
    style = draw(st.sampled_from(["py", "py2", "json", "yaml", "env", "export", "ini", "dict", "environ", "nbjson", "kw", "magic"]))
    text = {
        "py": f'{name} = "{v}"', "py2": f"{name}='{v}'", "json": json.dumps({name: v}, indent=draw(st.sampled_from([None, 2]))),
        "yaml": f"{name}: {v}", "env": f"{name}={v}", "export": f"export {name}={v}",
        "ini": f"{name} = {v}", "dict": "{" + f"'{name}': '{v}', 'x': 1" + "}",
        "environ": f'os.environ["{name}"] = "{v}"',
        "nbjson": json.dumps({"cells": [{"source": [f'{name} = "{v}"\n', "print(1)"]}]}),
        "kw": f'client = Client({name}="{v}", timeout=3)', "magic": f"%env {name}={v}",
    }[style]
    if style in ("yaml", "env", "export", "ini", "magic"):
        sep = "\n"  # unquoted values are recognised at the start of a line only
    return text, v, sep


PROSE = st.text(alphabet=string.ascii_letters + string.digits + " .,;!?()[]{}'\"", max_size=60)


@PROP
@given(planted(), PROSE, PROSE)
def test_planted_secrets_are_hidden_and_the_hidden_text_is_stable(p, before, after):
    text, secret, sep = p
    full = before + sep + text + sep + after
    # surrounding text must not be able to create or contain the secret by itself
    assume(secret not in before + after and "-----" not in before + after)
    out, n = redact(full)
    assert secret not in out, (full, out)
    assert n >= 1
    assert redact(out) == (out, 0)  # idempotent
    assert redact(full) == (out, n)  # deterministic


@PROP
@given(st.one_of(
    st.text(max_size=300),
    st.lists(st.sampled_from(
        ["password", "token", "secret", "api_key", " = ", ": ", "=", '"', "'", "\n", "x1", "Bearer ", "eyJ",
         "AKIA", "://", "@", "-----BEGIN PRIVATE KEY-----", "--password ", "export ", "ab12", "[hidden: token]", "\\",
         "# c "]), max_size=40).map("".join)))
def test_redaction_is_idempotent(text):
    once = redact(text)[0]
    assert redact(once) == (once, 0), (text, once)


# --- performance (property 7) -----------------------------------------------------------------

MB = 1_000_000
ADVERSARIAL = {
    "jwt_starts": "eyJ" * (MB // 3),
    "jwt_dots": "eyJabcde." * (MB // 9),
    "keyword_run": "token" * (MB // 5),
    "keyword_assign": 'token="' * (MB // 7),
    "keyword_space": "token" + " " * MB,
    "keyword_underscore": "api_key" + "_" * MB,
    "keywords_spaced": "password   " * (MB // 11),
    "aws_starts": "AKIA" * (MB // 4),
    "pem_starts": "-----BEGIN PRIVATE KEY-----" * (MB // 27),
    "pem_no_private": "-----BEGIN " + "A " * (MB // 2),
    "url_starts": "http://u:" * (MB // 9),
    "scheme_run": "a" * MB + "://",
    "url_long_pw": "http://u:" + "p" * MB,
    "bearer_run": "Authorization: Bearer " * (MB // 22),
    "flag_run": "--password " * (MB // 11),
    "line_value": "password: " + "a " * (MB // 2),
    "many_lines": "password=\n" * (MB // 10),
    "sk_run": "sk-" * (MB // 3),
    "sk_long": "sk-" + "a" * MB,
    "ghp_long": "ghp_" + "a" * MB,
    "quotes": '"' * MB,
    "backslashes": "\\" * MB,
    "plain": "x" * MB,
    "unicode": "é" * (MB // 2),
}


def test_linear_time_on_adversarial_input():
    for name, text in ADVERSARIAL.items():
        t0 = time.perf_counter()
        out, _ = redact(text)
        assert time.perf_counter() - t0 < 3.0, name
        assert redact(out)[0] == out, name


# --- events (property 5) ---------------------------------------------------------------------

SECRET = "Qv7xTz9LmW3pKd8Hs2Nb"
PLANT = f'api_key = "{SECRET}"'
HIDDEN_LINE = 'api_key = "[hidden: api key]"'


def _item(text):
    it = output_item(text)
    it["searchText"] = text
    return it


def _cell(cell_id, extra=None):
    c = {"cellId": cell_id, "cellType": "code", "code": PLANT, "revision": 1,
         "metadata": {"note": PLANT, "nested": {"list": [PLANT, 1, None, {"deep": PLANT}]}, "n": 5, "flag": True},
         "outputs": [{"runId": f"pasted:{cell_id}", "items": [_item(PLANT)]}]}
    if extra:
        c.update(extra)
    return c


def planted_events():
    """One event of every type, with the secret in every text field."""
    s = Scenario()
    cells = [_cell("c1"), _cell("c2")]
    s._emit("notebook_snapshot", {"path": "analysis.ipynb", "notebookMetadata": {"k": PLANT, "sub": {"x": [PLANT]}},
                                  "kernelName": "python3", "language": "python", "cells": cells},
            revs={"c1": 1, "c2": 1})
    s._emit("cell_inserted", {"cellId": "c3", "index": 2, "cellType": "code", "code": PLANT, "revision": 1,
                              "metadata": {"m": PLANT}, "outputs": [_item(PLANT), _item("fine")]},
            revs={"c1": 1, "c2": 1, "c3": 1})
    s.move("c3", 2, 0)
    s.delete("c2")
    s.change_type("c3", "markdown")
    s.clear_cell_outputs("c3")
    s.edit("c1", PLANT)
    s.cell_metadata("c1", {"a": PLANT, "b": [PLANT]})
    s.notebook_metadata({"nb": PLANT})
    run = s.run("c1")
    for method in (s.output, s.replace_output):
        ev = method(run, "x")
        d = ev.model_dump(mode="json")
        d["payload"]["item"]["preview"] = PLANT
        d["payload"]["item"]["searchText"] = PLANT
        s.events[-1] = BlaymEvent.model_validate(d).root
    s.clear_outputs(run)
    s.finish(run)
    s.kernel_start()
    s.kernel_restart()
    s.rename("a.ipynb", "b.ipynb")
    s.copy("b.ipynb", "c.ipynb")
    s._emit("changed_outside_editor", {"path": "b.ipynb", "notebookMetadata": {"o": PLANT}, "kernelName": "python3",
                                       "language": "python", "cells": [_cell("c1")]},
            revs={"c1": 1}, authorHint="outside")
    s.chat("m1", f"here: {PLANT}")
    s.chat_update("m1", f"here: {PLANT} more")
    s.gap_start()
    s.gap_end()
    s.branch_from(s.tick, "try")
    return s.events


def test_redact_event_hides_the_secret_in_every_type():
    assert len({e.type for e in planted_events()}) == 24
    for ev in planted_events():
        before = ev.model_dump(mode="json")
        out = redact_event(ev)
        dumped = out.model_dump(mode="json")
        assert SECRET not in json.dumps(dumped), ev.type
        assert ev.model_dump(mode="json") == before  # original untouched
        assert BlaymEvent.model_validate(dumped).model_dump(mode="json") == dumped  # validates
        # whatever is not text stays: the only change is the planted line becoming its marker
        want = json.loads(json.dumps(before).replace(PLANT.replace('"', '\\"'), HIDDEN_LINE.replace('"', '\\"')))
        assert dumped == want, ev.type


def test_metadata_keys_are_never_touched():
    s = Scenario()
    s.start(cells=[("c1", "x")])
    s.cell_metadata("c1", {PLANT: "v", "password": "plain"})
    out = redact_event(s.events[-1])
    assert list(out.payload.metadata) == [PLANT, "password"]


def test_ids_hashes_and_names_are_not_touched():
    s = Scenario()
    s.start(cells=[("api_key = 'Zz9Zz9Zz9Zz9'", "x")], path="token='Abc123Abc12'.ipynb")
    ev = s.events[0]
    out = redact_event(ev)
    assert out.payload.path == ev.payload.path
    assert out.payload.cells[0].cellId == ev.payload.cells[0].cellId
    run_events = Scenario()
    run_events.start(cells=[("c1", "x")])
    r = run_events.run("c1")
    o = run_events.output(r, f"{PLANT}")
    out = redact_event(o)
    assert out.payload.item.contentHash == o.payload.item.contentHash
    assert out.payload.item.sizeBytes == o.payload.item.sizeBytes
    assert out.payload.item.preview == HIDDEN_LINE


# --- rebuild property (property 6) ------------------------------------------------------------


def _plant_into(event, tag):
    d = event.model_dump(mode="json")
    secret = f'token = "Zk{tag}Pq8Lm3Wx"'

    def walk(o, key=None):
        if isinstance(o, dict):
            return {k: walk(v, k) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v, key) for v in o]
        if isinstance(o, str) and key in ("code", "body", "preview", "searchText"):
            return o + "\n" + secret
        return o

    d["payload"] = walk(d["payload"])
    return BlaymEvent.model_validate(d).root


def _oracle(state_dump):
    """The same hiding applied to a rebuilt state, written without redact_event."""
    d = json.loads(json.dumps(state_dump))
    hide = lambda s: redact(s)[0]  # noqa: E731

    def groups(gs):
        for g in gs:
            for it in g["items"]:
                it["preview"] = hide(it["preview"])
                if it.get("searchText") is not None:
                    it["searchText"] = hide(it["searchText"])

    def values(o):
        if isinstance(o, str):
            return hide(o)
        if isinstance(o, dict):
            return {k: values(v) for k, v in o.items()}
        if isinstance(o, list):
            return [values(v) for v in o]
        return o

    for c in d["notebook"]["cells"]:
        c["code"] = hide(c["code"])
        c["metadata"] = values(c["metadata"])
        groups(c["outputs"])
    d["notebook"]["metadata"] = values(d["notebook"]["metadata"])
    groups(d["detachedOutputs"])
    for m in d["chat"]:
        m["body"] = hide(m["body"])
    return d


@settings(max_examples=1000 if DEEP else 60)
@given(histories(max_events=MAX_EVENTS))
def test_rebuilding_redacted_events_equals_redacting_the_rebuilt_state(h):
    originals = [_plant_into(e, i) for i, e in enumerate(h.events)]
    redacted = [redact_event(e) for e in originals]
    assert all("Zk" not in json.dumps(e.model_dump(mode="json")) for e in redacted)
    a, b = Rebuilder(interval=5), Rebuilder(interval=5)
    a.add(originals)
    b.add(redacted)
    for branch, tick in _positions(h):
        ra, rb = a.state_at(branch, tick), b.state_at(branch, tick)
        assert ra.status == "ok", (branch, tick, ra)
        assert rb.status == "ok", (branch, tick, rb)  # no revision or run rule broken
        assert rb.model_dump(mode="json")["state"] == _oracle(ra.model_dump(mode="json")["state"]), (branch, tick)
    planted_count = sum("Zk" in json.dumps(e.model_dump(mode="json")) for e in originals)
    changed = sum(o.model_dump(mode="json") != r.model_dump(mode="json") for o, r in zip(originals, redacted))
    assert changed == planted_count  # the hiding really ran on every event that held the secret
