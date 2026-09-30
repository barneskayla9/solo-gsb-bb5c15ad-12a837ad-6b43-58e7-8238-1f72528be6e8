"""End-to-end API behavior for the batch-seal (factory archive) endpoints."""


def _event(rev, parent, start, end, value=None, withdrawn=False, key=None,
           batch="B1", sensor="S1", unit="mm"):
    return {
        "batch_id": batch,
        "sensor_id": sensor,
        "revision_id": rev,
        "parent_id": parent,
        "idempotency_key": key or f"k-{sensor}-{rev}",
        "start_ns": start,
        "end_ns": end,
        "value": value,
        "unit": unit,
        "withdrawn": withdrawn,
    }


def _post(client, event, batch="B1"):
    return client.post(f"/api/v1/batches/{batch}/events", json=event)


def _timeline_hash(client, batch="B1"):
    return client.get(f"/api/v1/batches/{batch}/result").json()["timeline_hash"]


def _seal(client, seal_id="SEAL-1", batch="B1", timeline_hash=None, **overrides):
    body = {
        "batch_id": batch,
        "seal_id": seal_id,
        "timeline_hash": (
            timeline_hash if timeline_hash is not None
            else _timeline_hash(client, batch)
        ),
    }
    body.update(overrides)
    return client.post(f"/api/v1/batches/{batch}/seals", json=body)


def _complete_batch(client, batch="B1"):
    """Two grounded revisions; the batch is complete afterwards."""
    _post(client, _event("v1", "ROOT", 0, 300, value=1.0, key="k1", batch=batch),
          batch=batch)
    _post(client, _event("v2", "v1", 100, 200, value=3.5, key="k2", batch=batch),
          batch=batch)


def test_seal_complete_batch_returns_evidence(client):
    _complete_batch(client)
    r = _seal(client)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "sealed"
    assert body["batch_id"] == "B1" and body["seal_id"] == "SEAL-1"
    assert body["timeline_hash"] == _timeline_hash(client)
    assert len(body["evidence_digest"]) == 64
    assert body["evidence_digest"] != body["timeline_hash"]
    # Every revision of the batch is archived in deterministic order.
    assert [rev["revision_id"] for rev in body["revisions"]] == ["v1", "v2"]
    assert body["revisions"][0] == {
        "sensor_id": "S1",
        "revision_id": "v1",
        "parent_id": "ROOT",
        "start_ns": 0,
        "end_ns": 300,
        "value": 1.0,
        "unit": "mm",
        "withdrawn": False,
    }
    # The folded result at seal time is archived with the revisions.
    assert body["result"]["complete"] is True
    assert body["result"]["timeline_hash"] == body["timeline_hash"]
    assert body["result"]["revisions_total"] == 2


def test_seal_replay_returns_original_snapshot(client):
    _complete_batch(client)
    first = _seal(client).json()
    assert first["status"] == "sealed"
    again = _seal(client).json()
    assert again["status"] == "duplicate"
    # Only the status differs; the archived evidence is identical.
    assert again == {**first, "status": "duplicate"}


def test_seal_replay_after_batch_evolution_returns_original(client):
    _complete_batch(client)
    sealed_hash = _timeline_hash(client)
    first = _seal(client, timeline_hash=sealed_hash).json()
    # The live batch moves on after the seal.
    _post(client, _event("v3", "v2", 120, 180, value=9.0, key="k3"))
    assert _timeline_hash(client) != sealed_hash
    # Replaying the original request still returns the original snapshot,
    # even though the sealed hash no longer matches the live batch.
    replay = _seal(client, timeline_hash=sealed_hash).json()
    assert replay["status"] == "duplicate"
    assert replay == {**first, "status": "duplicate"}
    assert len(replay["revisions"]) == 2
    assert replay["result"]["revisions_total"] == 2


def test_seal_same_id_different_hash_conflicts(client):
    _post(client, _event("v1", "ROOT", 0, 300, value=1.0, key="k1"))
    first_hash = _timeline_hash(client)
    assert _seal(client, timeline_hash=first_hash).status_code == 200
    _post(client, _event("v2", "v1", 100, 200, value=3.5, key="k2"))
    r = _seal(client, timeline_hash=_timeline_hash(client))
    assert r.status_code == 409
    # The original seal is untouched by the conflicting attempt.
    kept = client.get("/api/v1/batches/B1/seals/SEAL-1").json()
    assert kept["timeline_hash"] == first_hash


def test_seal_ids_are_scoped_per_batch(client):
    _complete_batch(client, batch="B1")
    _complete_batch(client, batch="B2")
    assert _seal(client, batch="B1").status_code == 200
    # The same seal id may be used in another batch with its own hash.
    assert _seal(client, batch="B2").status_code == 200


def test_seal_unknown_batch_404(client):
    r = client.post(
        "/api/v1/batches/NOPE/seals",
        json={"batch_id": "NOPE", "seal_id": "S1", "timeline_hash": "0" * 64},
    )
    assert r.status_code == 404


def test_seal_incomplete_batch_rejected_without_record(client):
    # v2 waits on the missing parent v1: the batch has a gap.
    _post(client, _event("v2", "v1", 0, 10, value=2.0, key="k2"))
    r = _seal(client, timeline_hash=_timeline_hash(client))
    assert r.status_code == 409
    # A rejected seal leaves no record behind.
    assert client.get("/api/v1/batches/B1/seals/SEAL-1").status_code == 404


def test_seal_batch_with_cycle_rejected(client):
    _post(client, _event("a", "b", 0, 10, value=1.0, key="ka"))
    _post(client, _event("b", "a", 0, 10, value=2.0, key="kb"))
    assert _seal(client).status_code == 409
    assert client.get("/api/v1/batches/B1/seals/SEAL-1").status_code == 404


def test_seal_stale_hash_rejected_without_record(client):
    _post(client, _event("v1", "ROOT", 0, 10, value=1.0, key="k1"))
    r = _seal(client, timeline_hash="0" * 64)
    assert r.status_code == 409
    assert client.get("/api/v1/batches/B1/seals/SEAL-1").status_code == 404


def test_seal_after_backfill_succeeds(client):
    _post(client, _event("v2", "v1", 100, 200, value=3.5, key="k2"))
    assert _seal(client).status_code == 409  # still incomplete
    _post(client, _event("v1", "ROOT", 0, 300, value=1.0, key="k1"))
    r = _seal(client)
    assert r.status_code == 200
    assert r.json()["status"] == "sealed"
    assert r.json()["result"]["complete"] is True


def test_seal_is_immutable_and_lookup_reports_drift(client):
    _complete_batch(client)
    _seal(client)
    sealed = client.get("/api/v1/batches/B1/seals/SEAL-1").json()
    assert sealed["current_consistent"] is True
    assert sealed["current_evidence_digest"] == sealed["evidence_digest"]
    assert sealed["current_timeline_hash"] == sealed["timeline_hash"]

    # Later revisions change the live result but never rewrite the seal.
    _post(client, _event("v3", "v2", 120, 180, value=9.0, key="k3"))
    after = client.get("/api/v1/batches/B1/seals/SEAL-1").json()
    assert after["current_consistent"] is False
    assert after["current_timeline_hash"] != after["timeline_hash"]
    assert after["current_evidence_digest"] != after["evidence_digest"]
    assert after["revisions"] == sealed["revisions"]
    assert after["result"] == sealed["result"]
    assert after["evidence_digest"] == sealed["evidence_digest"]
    assert after["timeline_hash"] == sealed["timeline_hash"]


def test_seal_lookup_unknown_404(client):
    _post(client, _event("v1", "ROOT", 0, 10, value=1.0, key="k1"))
    assert client.get("/api/v1/batches/B1/seals/NOPE").status_code == 404


def test_seal_revisions_sorted_across_sensors(client):
    _post(client, _event("b", "ROOT", 0, 10, value=2.0, sensor="S2", key="kb"))
    _post(client, _event("a", "ROOT", 0, 10, value=1.0, sensor="S1", key="ka"))
    body = _seal(client).json()
    assert [(r["sensor_id"], r["revision_id"]) for r in body["revisions"]] == [
        ("S1", "a"),
        ("S2", "b"),
    ]


def test_seal_archives_withdrawn_revision(client):
    _post(client, _event("v1", "ROOT", 0, 100, value=1.0, key="k1"))
    _post(client, _event("w1", "v1", 10, 20, withdrawn=True, unit=None, key="kw"))
    body = _seal(client).json()
    assert body["status"] == "sealed"
    w1 = next(r for r in body["revisions"] if r["revision_id"] == "w1")
    assert w1["withdrawn"] is True
    assert w1["value"] is None and w1["unit"] is None
    seg = body["result"]["sensors"][0]["segments"][1]
    assert seg["state"] == "withdrawn" and seg["value"] is None


def test_seal_digest_deterministic_across_arrival_order(client):
    events = [
        _event("r", "ROOT", 0, 100, value=1.0, key="kr"),
        _event("a", "r", 0, 50, value=2.0, key="ka"),
        _event("b", "r", 25, 75, value=3.0, key="kb"),
        _event("c", "a", 40, 60, withdrawn=True, key="kc"),
    ]
    digests = []
    for permutation in (events, list(reversed(events))):
        client.post("/admin/reset")
        for event in permutation:
            assert _post(client, event).status_code == 200
        r = _seal(client)
        assert r.status_code == 200
        digests.append(r.json()["evidence_digest"])
    assert digests[0] == digests[1]


def test_reset_clears_seals(client):
    _complete_batch(client)
    _seal(client)
    r = client.post("/admin/reset")
    assert r.json()["seals_removed"] == 1
    assert r.json()["events_removed"] == 2
    assert client.get("/api/v1/batches/B1/seals/SEAL-1").status_code == 404


def test_seal_path_body_batch_mismatch_422(client):
    _post(client, _event("v1", "ROOT", 0, 10, value=1.0, key="k1"))
    body = {
        "batch_id": "OTHER",
        "seal_id": "SEAL-1",
        "timeline_hash": _timeline_hash(client),
    }
    assert client.post("/api/v1/batches/B1/seals", json=body).status_code == 422


def test_seal_validation_errors(client):
    good_hash = "a" * 64
    assert _seal(client, seal_id="", timeline_hash=good_hash).status_code == 422
    assert _seal(client, timeline_hash="not-a-hash").status_code == 422
    assert _seal(client, timeline_hash="A" * 64).status_code == 422
    assert _seal(client, timeline_hash="a" * 63).status_code == 422
    assert _seal(client, timeline_hash=good_hash, extra=1).status_code == 422
    r = client.post(
        "/api/v1/batches/B1/seals",
        json={"batch_id": "B1", "seal_id": "S"},  # missing timeline_hash
    )
    assert r.status_code == 422
