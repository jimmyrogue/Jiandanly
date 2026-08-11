from shejane_runtime.presentation import project_run_presentation


def test_projects_summary_and_progress_from_the_same_round() -> None:
    snapshot = project_run_presentation(
        run={"id": "run-1", "status": "running"},
        assistant_item=None,
        events=[
            _event(
                1,
                "assistant.round.committed",
                {
                    "round_id": "model-call-1",
                    "text": "I’ll inspect the file.",
                    "reasoning_summary": "The repository structure determines the next read.",
                    "tool_call_ids": ["call-1"],
                },
            )
        ],
        tool_receipts=[],
        event_high_watermark=1,
    )

    assert [item["kind"] for item in snapshot["items"]] == [
        "reasoning_summary",
        "progress",
    ]


def test_projects_summary_when_the_round_has_no_display_text() -> None:
    snapshot = project_run_presentation(
        run={"id": "run-1", "status": "running"},
        assistant_item=None,
        events=[
            _event(
                1,
                "assistant.round.committed",
                {
                    "round_id": "model-call-1",
                    "text": "",
                    "reasoning_summary": "The repository structure determines the next read.",
                    "tool_call_ids": ["call-1"],
                },
            )
        ],
        tool_receipts=[],
        event_high_watermark=1,
    )

    assert [item["kind"] for item in snapshot["items"]] == ["reasoning_summary"]


def test_tool_item_identity_stays_stable_when_a_receipt_appears() -> None:
    events = [
        _event(
            1,
            "tool.requested",
            {
                "tool_call_id": "call-1",
                "tool": "read_file",
                "arguments": {"path": "/workspace/README.md"},
            },
        )
    ]
    before = project_run_presentation(
        run={"id": "run-1", "status": "running"},
        assistant_item=None,
        events=events,
        tool_receipts=[],
        event_high_watermark=1,
    )
    after = project_run_presentation(
        run={"id": "run-1", "status": "running"},
        assistant_item=None,
        events=events,
        tool_receipts=[
            {
                "operation_id": "toolop-1",
                "tool_call_id": "call-1",
                "tool_name": "read_file",
                "status": "running",
                "risk": "read_only",
                "arguments_json": '{"path":"/workspace/README.md"}',
                "created_at": "2026-08-04T00:00:01Z",
                "updated_at": "2026-08-04T00:00:02Z",
                "completed_at": None,
            }
        ],
        event_high_watermark=1,
    )

    assert before["items"][0]["id"] == after["items"][0]["id"] == "tool-call:call-1"
    assert before["items"][0]["display_target"] == "README.md"
    assert after["items"][0]["display_target"] == "README.md"


def test_tool_item_projects_safe_web_target_and_failure_detail() -> None:
    events = [
        _event(
            1,
            "tool.requested",
            {
                "tool_call_id": "call-1",
                "tool": "web.fetch",
                "arguments": {"url": "https://www.bochk.com/a?token=must-not-appear"},
            },
        ),
        _event(
            2,
            "tool.failed",
            {
                "tool_call_id": "call-1",
                "tool": "web.fetch",
                "error_code": "Authorization:must-not-appear",
                "message": "404 Not Found for https://bochk.com/?token=must-not-appear",
            },
        ),
    ]

    snapshot = project_run_presentation(
        run={"id": "run-1", "status": "failed"},
        assistant_item=None,
        events=events,
        tool_receipts=[
            {
                "operation_id": "toolop-1",
                "tool_call_id": "call-1",
                "tool_name": "web.fetch",
                "status": "failed",
                "risk": "read_only",
                "arguments_json": '{"url":"https://www.bochk.com/a?token=must-not-appear"}',
                "error_type": "HTTPStatusError",
                "created_at": "2026-08-04T00:00:01Z",
                "updated_at": "2026-08-04T00:00:02Z",
                "completed_at": "2026-08-04T00:00:02Z",
            }
        ],
        event_high_watermark=2,
    )

    item = snapshot["items"][0]
    assert item["display_target"] == "bochk.com"
    assert item["display_target_kind"] == "host"
    assert item["failure_detail"] == "404 Not Found"
    assert "must-not-appear" not in str(item)


def test_tool_item_omits_sensitive_free_form_arguments() -> None:
    snapshot = project_run_presentation(
        run={"id": "run-1", "status": "running"},
        assistant_item=None,
        events=[
            _event(
                1,
                "tool.requested",
                {
                    "tool_call_id": "call-1",
                    "tool": "shell.run",
                    "arguments": {"command": "curl -H 'Authorization: secret-token'"},
                },
            )
        ],
        tool_receipts=[],
        event_high_watermark=1,
    )

    item = snapshot["items"][0]
    assert item["display_target"] is None
    assert "secret-token" not in str(item)


def test_ignores_events_and_receipts_from_non_main_execution_namespaces() -> None:
    snapshot = project_run_presentation(
        run={"id": "run-1", "status": "running"},
        assistant_item=None,
        events=[
            _event(
                1,
                "tool.requested",
                {
                    "tool_call_id": "call-1",
                    "tool": "read_file",
                    "execution_namespace": "child:researcher",
                },
            )
        ],
        tool_receipts=[
            {
                "operation_id": "toolop-child",
                "execution_namespace": "child:researcher",
                "tool_call_id": "call-1",
                "tool_name": "read_file",
                "status": "completed",
                "risk": "read_only",
                "arguments_json": "{}",
                "created_at": "2026-08-04T00:00:01Z",
                "updated_at": "2026-08-04T00:00:02Z",
                "completed_at": "2026-08-04T00:00:02Z",
            }
        ],
        event_high_watermark=1,
    )

    assert snapshot["items"] == []


def test_projects_decision_artifact_and_terminal_notice() -> None:
    events = [
        _event(1, "permission.required", {"request_id": "permission-1", "tool": "shell.run"}),
        _event(
            2,
            "permission.resolved",
            {"request_id": "permission-1", "decision": "approve"},
        ),
        _event(
            3,
            "artifact.created",
            {"artifact_id": "artifact-1", "title": "Report", "media_type": "text/markdown"},
        ),
        _event(4, "run.failed", {"error": "Provider disconnected"}),
    ]

    snapshot = project_run_presentation(
        run={"id": "run-1", "status": "failed"},
        assistant_item=None,
        events=events,
        tool_receipts=[],
        wait_candidates=[
            {
                "id": "permission-1",
                "kind": "tool_review",
                "status": "resolved",
                "payload_json": '{"tool_name":"shell.run"}',
                "created_at": "2026-08-04T00:00:01Z",
                "resolved_at": "2026-08-04T00:00:02Z",
            }
        ],
        artifacts=[
            {
                "id": "artifact-1",
                "title": "Report",
                "content_type": "text/markdown",
                "created_at": "2026-08-04T00:00:03Z",
            }
        ],
        event_high_watermark=4,
    )

    assert [item["kind"] for item in snapshot["items"]] == [
        "approval",
        "artifact",
        "notice",
    ]
    decision, artifact, notice = snapshot["items"]
    assert decision["status"] == "completed"
    assert decision["summary"] == "shell.run"
    assert artifact["id"] == "artifact:artifact-1"
    assert notice["status"] == "failed"
    assert notice["message"] == "Run failed"


def _event(seq: int, event_type: str, payload: dict) -> dict:
    return {
        "id": f"event-{seq}",
        "run_id": "run-1",
        "seq": seq,
        "event_type": event_type,
        "payload": payload,
        "created_at": f"2026-08-04T00:00:0{seq}Z",
    }
