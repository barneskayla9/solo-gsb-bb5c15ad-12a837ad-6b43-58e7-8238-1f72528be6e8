"""Engine unit tests: seal evidence snapshot and digest determinism."""

from app.engine import (
    evaluate_batch,
    hash_seal_evidence,
    revision_to_dict,
    sealed_revision_list,
)
from app.storage import BatchStore, StoredEvent


def _ev(rev, parent, start=0, end=10, value=1.0, withdrawn=False,
        batch="B", sensor="S", key=None, unit="u"):
    return StoredEvent(
        batch_id=batch,
        sensor_id=sensor,
        revision_id=rev,
        parent_id=parent,
        idempotency_key=key or f"k-{rev}",
        start_ns=start,
        end_ns=end,
        value=None if withdrawn else float(value),
        unit=unit,
        withdrawn=withdrawn,
    )


def _store(*events):
    store = BatchStore()
    for event in events:
        store.events[event.revision_id] = event
        store.revision_sensor[event.revision_id] = event.sensor_id
    return store


def test_sealed_revision_list_is_sorted_and_complete():
    store = _store(
        _ev("b", "ROOT", sensor="S2"),
        _ev("a", "ROOT", sensor="S1"),
        _ev("c", "a", sensor="S1"),
    )
    revisions = sealed_revision_list(store)
    assert [(e.sensor_id, e.revision_id) for e in revisions] == [
        ("S1", "a"),
        ("S1", "c"),
        ("S2", "b"),
    ]


def test_revision_to_dict_drops_idempotency_key():
    doc = revision_to_dict(_ev("v", "ROOT", start=5, end=9, value=2.5))
    assert doc == {
        "sensor_id": "S",
        "revision_id": "v",
        "parent_id": "ROOT",
        "start_ns": 5,
        "end_ns": 9,
        "value": 2.5,
        "unit": "u",
        "withdrawn": False,
    }
    # A tombstone submitted without value/unit archives both as null.
    tombstone = revision_to_dict(_ev("w", "v", withdrawn=True, unit=None))
    assert tombstone["withdrawn"] is True
    assert tombstone["value"] is None and tombstone["unit"] is None


def test_seal_digest_is_stable_and_covers_revisions_and_result():
    store = _store(_ev("v1", "ROOT"), _ev("v2", "v1", start=2, end=8, value=3.0))
    result = evaluate_batch("B", store)
    revisions = sealed_revision_list(store)
    digest = hash_seal_evidence(revisions, result)
    assert len(digest) == 64
    # Same evidence, re-derived: identical digest.
    assert hash_seal_evidence(sealed_revision_list(store), result) == digest
    # Arrival/idempotency-key differences do not affect the digest.
    store2 = _store(
        _ev("v2", "v1", start=2, end=8, value=3.0, key="other"),
        _ev("v1", "ROOT", key="other2"),
    )
    assert hash_seal_evidence(
        sealed_revision_list(store2), evaluate_batch("B", store2)
    ) == digest
    # A changed revision changes the digest.
    changed = _store(_ev("v1", "ROOT"), _ev("v2", "v1", start=2, end=8, value=4.0))
    assert hash_seal_evidence(
        sealed_revision_list(changed), evaluate_batch("B", changed)
    ) != digest
    # An added revision changes the digest even where the fold is unchanged.
    extra = _store(
        _ev("v1", "ROOT"),
        _ev("v2", "v1", start=2, end=8, value=3.0),
        _ev("v3", "v2", start=2, end=8, value=3.0),
    )
    assert hash_seal_evidence(
        sealed_revision_list(extra), evaluate_batch("B", extra)
    ) != digest
