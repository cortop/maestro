"""RB-4: step_id's pre-hash encoding must be injective (no delimiter injection),
backward-compatible for every step id already recorded, and reject a control
character in `action` (and in `qa-verdict --axis`) at the boundary.
"""
import pytest

from maestro import cli, event_log, ops, snapshot as snap_mod, store
from maestro.idempotency import step_id

SPEC_TEMPLATE = """\
# {key}: {title}

approval_tier: 1
priority: 2

## Intent
{intent}

## Acceptance criteria
- [ ] {ac}
"""


def _write_spec(home, key, *, title="Test ticket", intent="do the thing", ac="build the widget"):
    store.atomic_write(store.spec_path(home, key),
                       SPEC_TEMPLATE.format(key=key, title=title, intent=intent, ac=ac))


def _create(cfg, key):
    _write_spec(cfg.home, key)
    event_log.append(cfg.home, key, "TicketCreated", {"title": "Test ticket", "source": "test"}, actor="d")
    return snap_mod.rebuild(cfg.home, key)


# ---------------------------------------------------------------------------
# AC1 -- injective: two distinct tuples never collide, including the
# delimiter-injection reproduction the ticket describes.
# ---------------------------------------------------------------------------

def test_delimiter_injection_no_longer_collides():
    """Reproduction from the ticket: a \\x1f in `key` used to make two distinct
    tuples hash identically. It must not anymore."""
    a = step_id("K\x1fp", "q", 1, "a")
    b = step_id("K", "p\x1fq", 1, "a")
    assert a != b


def test_delimiter_boundary_shift_between_key_and_phase_does_not_collide():
    # \x1f in `action` is itself rejected (a control character -- see the
    # control-char tests below), so shift the boundary between `key` and
    # `phase` instead, at a different observed_seq than the ticket's own
    # reproduction -- proving this isn't a one-off fix for that exact tuple.
    a = step_id("K\x1fp", "q", 5, "clean-action")
    b = step_id("K", "p\x1fq", 5, "clean-action")
    assert a != b


def test_delimiter_free_tuples_still_distinguish_by_field_content():
    # Sanity: the fix shouldn't have broken ordinary (already-injective) cases.
    assert step_id("K1", "q", 1, "a") != step_id("K2", "q", 1, "a")
    assert step_id("K", "q1", 1, "a") != step_id("K", "q2", 1, "a")
    assert step_id("K", "q", 1, "a") != step_id("K", "q", 2, "a")


def test_empty_key_does_not_collide_with_a_crafted_length_prefixed_tuple():
    """Adversarial case: an empty `key` makes the plain-join payload start
    with "\\x1f" too (the field before it is empty). If the branch marker
    were only a single "\\x1f", a plain-join payload could be crafted to look
    exactly like a length-prefixed payload for a *different* tuple -- e.g.
    key="", phase="5:", observed_seq=45, action="X0:1:70:" joins to
    "\\x1f5:\\x1f45\\x1fX0:1:70:", which a single-"\\x1f" marker scheme would
    also produce for key="\\x1f45\\x1fX", phase="", observed_seq=7, action="".
    The three-separator marker rules this out: a plain-join payload always
    has exactly three "\\x1f" bytes; a length-prefixed one always has at
    least four."""
    a = step_id("", "5:", 45, "X0:1:70:")
    b = step_id("\x1f45\x1fX", "", 7, "")
    assert a != b


# ---------------------------------------------------------------------------
# AC2 -- digest stability: separator-free inputs hash exactly as before.
# Digests below were captured from the pre-fix implementation.
# ---------------------------------------------------------------------------

def test_digest_stable_for_separator_free_inputs():
    assert step_id("RB-4", "implementing", 7, "phase:awaiting-ci") == "f675a45a5ad7101b"
    assert step_id("T-1", "ready", 0, "note-transition-implementing") == "68d2d8182516fd13"
    assert step_id("K", "q", 1, "a") == "b8aced07eb09d9c2"
    assert step_id("T-1", "implementing", 3, "fail") == "299a074e68f4533c"


# ---------------------------------------------------------------------------
# AC3 -- control characters in `action` (and qa-verdict's --axis) are rejected.
# ---------------------------------------------------------------------------

def test_control_char_in_action_is_rejected():
    with pytest.raises(store.MaestroError):
        step_id("K", "q", 1, "bad\x01action")
    with pytest.raises(store.MaestroError):
        step_id("K", "q", 1, "bad\x1finjected\x01action")


def test_qa_verdict_axis_with_control_char_rejected_via_real_cli(cfg):
    """`--axis` is `choices`-restricted to {spec, standards} in the CLI parser
    itself, so a control character there is already rejected before
    `record_qa_verdict`/`step_id` ever run -- this is the CLI boundary the
    ticket asks for, exercised end to end via the real `maestro` command."""
    home = cfg.home
    _create(cfg, "T-1")
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--home", str(home), "qa-verdict", "T-1", "--ac", "1",
                  "--verdict", "pass", "--evidence", "e", "--axis", "spec\x01"])
    assert exc_info.value.code != 0


def test_step_id_itself_rejects_a_control_char_that_reaches_action_via_axis():
    """Belt-and-braces, independent of the CLI's `--axis` choices restriction
    above: `record_qa_verdict` folds `axis` into the `action` string it hands
    to `step_id` (`f"qaverdict-{axis}-{h}-{verdict}"`, ops.py). If that axis
    check were ever loosened or bypassed (a direct `ops.record_qa_verdict`
    call, not through the CLI), `step_id` itself is the second, unconditional
    line of defense -- prove it directly against the exact action shape
    `record_qa_verdict` composes."""
    with pytest.raises(store.MaestroError):
        step_id("T-1", "implementing", 3, "qaverdict-spec\x01-deadbeefcafef00d-pass")


def test_step_id_guard_is_the_last_line_of_defense_via_real_cli(cfg, monkeypatch):
    """No current `ops.py` call site ever hands `step_id` an `action` built from
    unvalidated CLI text -- every one is a literal or an already-constrained
    enum/int/hash value (verified by inspection: `note-transition-<phase>`,
    `phase:<phase>`, `force-ac-override`, `warn-acs-unverified`,
    `local-write-backup`, `qaverdict-<axis>-<ac_hash>-<verdict>`,
    `requeue:<int>`, `implturn:<int>`, `fail` -- `axis`/`verdict` are
    `choices`-restricted, `<ac_hash>` is a content hash, the rest are literals
    or ints). So there is no live vulnerability to reproduce through
    `cli.main` as written today.

    What *is* real: `--axis`'s CLI-level `choices=sorted(ops.QA_AXES)` (built
    fresh from `ops.QA_AXES` on every `cli.main` call, see `build_parser`) and
    `record_qa_verdict`'s own `axis not in QA_AXES` check both derive from the
    same `QA_AXES` set. If that set were ever misconfigured to admit a value
    carrying a control character, both of those layers would wave it through
    unchanged -- `step_id`'s guard is the last one standing. Stress that,
    end to end through the real `cli.main` entrypoint (not a direct `step_id`
    call): widen `QA_AXES` to admit a control-char axis, confirm the CLI
    parser and `record_qa_verdict` both now accept it, and confirm `step_id`
    still refuses it, surfacing as `cli.main`'s standard non-zero exit."""
    home = cfg.home
    _create(cfg, "T-1")
    bad_axis = "spec\x01"
    monkeypatch.setattr(ops, "QA_AXES", {"spec", "standards", bad_axis})
    rc = cli.main(["--home", str(home), "qa-verdict", "T-1", "--ac", "1",
                   "--verdict", "pass", "--evidence", "e", "--axis", bad_axis])
    assert rc != 0


# ---------------------------------------------------------------------------
# AC5 -- digest width unchanged.
# ---------------------------------------------------------------------------

def test_digest_width_still_16_hex_chars():
    sid = step_id("a\x1fb", "q", 1, "x")
    assert len(sid) == 16
    int(sid, 16)  # still hex


# ---------------------------------------------------------------------------
# AC6 -- QA over a real temp MAESTRO_HOME through the real CLI: a repeated
# action is still deduped (crash-and-respawn safety, unaffected by the fix).
# ---------------------------------------------------------------------------

def test_repeated_action_still_deduped_via_real_cli(cfg):
    """Two reconcilers racing on the same frozen log compute the same step id
    for the same next action (the module docstring's guarantee); the event
    log's step-id dedup then collapses them to a single recorded effect.
    Simulated as two identical `maestro append` calls sharing a step id
    computed once via `step_id()` at a fixed (key, phase, observed_seq)."""
    home = cfg.home
    _create(cfg, "T-1")
    snap = snap_mod.load(home, "T-1")
    sid = step_id("T-1", snap.phase, snap.observed_seq, "note-race")

    rc1 = cli.main(["--home", str(home), "append", "T-1", "--type", "Note",
                     "--payload", '{"text":"first"}', "--step-id", sid])
    assert rc1 == 0
    n1 = len(event_log.read(home, "T-1"))

    # Same step id again (the "racing second reconciler"): must be a no-op.
    rc2 = cli.main(["--home", str(home), "append", "T-1", "--type", "Note",
                     "--payload", '{"text":"duplicate"}', "--step-id", sid])
    assert rc2 == 0
    n2 = len(event_log.read(home, "T-1"))
    assert n2 == n1
