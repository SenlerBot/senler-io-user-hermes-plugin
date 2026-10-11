# Connect incoming Senler events without a public receiver

Use webhook type `hermes`. Senler supplies the address; this plugin dials out.
No public IP, tunnel or inbound port is needed. MCP authorization remains user-scoped.

On native Windows, run the commands through `../scripts/run-relay.ps1 <action>`
(relative to this reference) with JSON stdin and `-EnableAutomation` for configure.
This preserves UTF-8 input/output in PowerShell 5. It invokes the same
`hermes senler relay` command and never places credentials in process arguments.

## Prepare

1. Resolve the project, event types and processing rules using User MCP. Read
   the actual available-events catalog; all subscribed types are supported.
   Save the actions the operator requested in plain-language instructions.
   Include requested channel and business filters in the saved operator policy.
   A request for automatic processing authorizes enabling this background adapter;
   do not ask for that same permission again.
2. Verify MCP with `hermes senler check`. Missing OAuth requires interactive
   `hermes senler connect`, never credentials pasted in chat.
3. Run `hermes senler relay identity` and `hermes senler relay status` in the
   same profile. Retain the stable `client_id`, `client_name`, saved webhook IDs
   and `local_revision`. These commands do not disclose credentials.
4. Discover and inspect list/create/update, bind-Hermes, test and Hermes-deliveries
   methods through MCP search/describe. Create must expose `kind=hermes`; binding
   must accept `client_id` and `client_name`. If absent, report that Senler API/User
   MCP needs the Relay update. Do not invent an endpoint or silently use HTTP.

## Create or reuse, connect and automatically activate

1. Reuse the intended saved subscription. If given a connection address, find
   the matching webhook in that project's list. To create, send a stable clear
   name, `kind: "hermes"` and selected `events`. Omit `url`, `signature_scheme`,
   `custom_headers` and `status`. The server saves a disabled subscription with
   automatic activation pending. After a timeout, list before retrying creation.
   Do not create overlapping workflows just because a new chat lacks context.
2. Call bind-Hermes for this exact project/webhook using the local identity.
   A conflict means another installation owns it; preserve it and explain.
   Reset only when the operator requests replacement.
3. Pass the **extracted API response object**, not the outer MCP wrapper, to
   `hermes senler relay configure --enable-automation` as JSON **stdin**:

   ```json
   {
     "connection": "<replace with the complete bind-Hermes response object>",
     "instructions": "<the operator's requested processing rules and actions>",
     "expected_local_revision": null
   }
   ```

   For an existing local subscription pass its previously read `local_revision`.
   A conflict means newer operator settings won; reconcile that intent before
   retrying. Never put `gateway_secret` in command-line arguments, logs or final
   chat text. Do not read or edit the SQLite store or OAuth token files.
   When only refreshing credentials for an existing subscription,
   omit `instructions` to preserve its saved operator policy and local pause.
4. If setup returns `gateway_restart_required`, use the installed Hermes gateway
   start/restart flow for this profile; inspect its CLI help when needed. Preserve
   other platform settings. A running Senler adapter discovers later subscription
   changes automatically. No public receiver or tunnel is needed.
5. Read the webhook until `hermes.verified_at` exists and, for a requested active
   workflow, `status` is `active`. Native Relay probes check the saved project,
   revision and **every selected event type**, without model or MCP calls.
   Senler activates after all probes are acknowledged. The test method can
   recheck a connected adapter. Do not claim success from a saved file alone
   or ask for a separate enable request. If offline, inspect gateway status
   and report the actual failure.

Reconnect preserves manual pause. To resume, an operator request authorizes
`status: "active"`; if verification is missing, activation waits for the check.
Resume locally with `hermes senler relay resume --enable-automation` and JSON
stdin containing the saved `webhook_id` and `expected_local_revision`. This
preserves the instructions and credentials without exposing them.
`hermes senler relay status` includes `processing_blocked_reason`, derived from
the current gateway configuration. A non-null reason explains why new events
cannot start. Keep the existing subscription and instructions; explain which
gateway setting needs correction. After correction, still-valid waiting events
continue automatically unless manually paused. Completed reports still deliver,
and event expiry still applies. A null reason does not prove the gateway is running.
Change where the agent executes by stopping and starting the gateway; do not
promise to move an in-progress task or automatically replay an interrupted one.
Event selection changes through MCP or the Cabinet synchronize over the
authenticated connection automatically. Matching pending events are preserved;
removed event types are cancelled with a visible result. Local instructions and
manual pause are not changed by server synchronization.

## Change processing instructions

For requests such as "add a condition" or "change how you handle these events",
reuse the saved webhook ID. Read its current rules with
`hermes senler relay instructions` and JSON stdin `{"webhook_id":"<saved ID>"}`.
The response includes `instructions`, project, events, pause state and
`local_revision`, without connection credentials.

Apply the operator's requested change to those saved instructions, preserving
unrelated rules. Send JSON stdin to `hermes senler relay update-instructions`:

```json
{
  "webhook_id": "<saved ID>",
  "instructions": "<complete updated instructions>",
  "expected_local_revision": 0
}
```

Use the revision just read, not the example value. On a conflict, read the rules
again and reconcile the requested change with the newer text. Do not blindly
retry the old replacement. These commands work without MCP authorization and
preserve credentials, event selection and manual pause. Do not rebind, recreate
the webhook or restart the gateway merely to edit rules. Waiting events use the
updated instructions; an already running attempt stops before subsequent tool
calls and its previously submitted actions may have completed.
Saving identical instructions keeps the current attempt running. Restoring old
instructions after a change does not revive an already cancelled attempt.
On Windows use `run-relay.ps1 instructions` / `update-instructions` with JSON
stdin, just as for the other actions; no `-EnableAutomation` flag is needed.

## Pause, remove, move and review

- Pause locally first: send `{"webhook_id":"<saved ID>"}` on stdin to
  `hermes senler relay pause`, then set this webhook `status: "disabled"`
  through MCP. If OAuth is unavailable, distinguish local pause from the pending
  server update. The adapter cancels processing and blocks subsequent tool calls;
  an already submitted action may finish and is never claimed to be undone.
  Cancellation is saved before stopping the gateway task. Resume continues the
  waiting queue; it never automatically restarts a cancelled attempt, even after
  a quick pause/resume. Check prior actions before any operator-requested retry.
- Remove in the same order: `relay remove`, then API delete. This deletes that
  local inbox too. Touch only the workflow the operator requested.
- Moving computers uses reset-Hermes after an operator replacement request,
  then binding on the new computer. Reset revokes the old key. One installation
  is allowed per subscription; separate workflows need distinct subscriptions
  and must avoid overlapping automatic actions.
- Review through Hermes-deliveries for the exact project/webhook, following
  cursors. `queued` means waiting in Senler, `received` means local durable receipt,
  and `processing` means the server granted one execution attempt. `completed`/
  `failed` are outcomes; `expired` means no result arrived within retention.
  Reports are limited to 16,000 characters; native
  session history contains the full transcript. Never rerun actions for a report.

Senler keeps up to 1,000 unfinished events per subscription for 24 hours from
enqueue, including events already received by a computer. Hermes
stores up to 1,000 pending events per profile and processes one at a time. Reports
and deduplication records are retained for seven days. Delivery may repeat after
failures. Completed local results are reported again without running the agent.
Before execution starts, connection failures leave the event waiting. A saved
attempt ID makes requesting permission to start repeatable after a lost reply
or reconnect; the model is invoked only after the grant is received and the
local execution boundary is saved. Once execution has begun, an interrupted
attempt is reported as failed for review, never automatically executed again.
Reports are delivered fairly across connected subscriptions; a paused or slow
connection cannot occupy the shared report batch. Reply hooks persist and inject the API idempotency key and reject
changed reply content or a different dialog for the same event. Other writes
still require their API-specific outcome checks before any operator-authorized retry.
At capacity, newly arriving events are skipped; Senler exposes the rejection
counter and last error. Check these when diagnosing missing events.
A running computer and gateway are required for prompt handling.
