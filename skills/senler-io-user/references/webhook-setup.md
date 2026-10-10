# Connect Senler events to this Hermes user

The helper is `../scripts/senler_webhook.py`, relative to this reference.
Run it with Hermes' own Python. In native Windows, `../scripts/run-helper.ps1`
finds Python beside `hermes.exe`, or accepts `-PythonPath`. Use JSON stdin for
all actions below; never pass secrets in command-line switches or print them.
The helper makes no Senler HTTP calls and never reads OAuth credentials.
For `webhook` and `result`, pass the API response object extracted from the MCP
result, not the outer MCP content/transport envelope.

For an existing connection's status, pause or removal, go to **Existing
connections** below. Those actions do not require a working public endpoint or
re-provisioning; a local pause must remain possible when OAuth is unavailable.

## Prerequisites and operator scope

1. Establish the project, selected event types and processing instruction from
   the current request. Resolve names with User MCP. Default to preparing drafts;
   unattended sending needs an explicit operator policy. Resolve any channel IDs
   through MCP. A workflow applies to one project; repeat for another project
   only when that additional scope is authorized.
2. Run `preflight`. The MCP connection must exist, webhook platform must be
   enabled and local gateway must be healthy. Check native OAuth with
   `hermes mcp test senler-io-user` and a read through User MCP. A health check
   does not establish public reachability or OAuth consent.
3. One-time operator provisioning is documented in the package's `docs/webhooks.md`.
   It grants only `mcp-senler-io-user` to routes owned by this plugin. Do not grant
   terminal, filesystem or additional MCP toolsets from a background event.
   The endpoint must already have a public HTTPS origin forwarding to Hermes.
   Do not invent a URL or install a tunnel/subscription to obtain one.
4. Discover project-webhook list/create/update/secret/test/events methods through
   MCP search, then inspect their schemas. Create must support `status` and
   `signature_scheme`; test must accept `event_type`. If those fields are absent,
   report that this Senler server needs the webhook update. Stop before creating
   an incompatible subscription. Use `diagnose_access` for missing methods.

## Create or resume, test, then activate automatically

Use a stable short lowercase `workflow` such as `support`; do not change it on
retries. First call `connections` with `{"project_id":"<resolved UUID>"}` to
discover saved bindings, including in a new chat. Follow `next_cursor` using
`after` if present. Reuse the matching workflow and webhook ID. If several saved
workflows could be intended, resolve which one the operator means. Do not invent
a new workflow name merely because this chat does not remember the old one.
Create an additional workflow only when a separate processing rule is intended;
check existing event/channel scopes to avoid duplicate automatic replies.

All subsequent helper JSON inputs contain `project_id` and `workflow`.

1. Run `plan`. It returns a stable route, public URL, Senler name and any local
   binding, plus a `revision` (null for a new connection). Keep that revision for
   `configure`; if another request paused or changed the connection in between,
   stop the stale setup and reconcile the newer user intent. Do not blindly
   refresh the revision to bypass the conflict. Read existing project webhooks
   through MCP. Reuse its recorded ID;
   after an interrupted create, match both the exact planned name and URL. If
   there is more than one match, resolve that conflict instead of guessing.
   On a create timeout, read the list before attempting another create.
2. If creating, call the discovered MCP create method with the planned name and
   URL, selected `events`, `status: "disabled"`,
   `signature_scheme: "standard-webhooks"`. For an existing owned connection,
   set status disabled before changing its destination/events. Preserve unrelated
   headers and settings. Do not adopt an unrelated subscription or switch the
   signature of an existing Senler-v1 subscription.
3. Get that webhook's secret through MCP. Run `configure` with:

   ```json
   {
     "project_id": "<resolved project UUID>",
     "workflow": "support",
     "expected_revision": "<revision from plan; JSON null for a new connection>",
     "webhook": "<replace with the complete successful create/update result object>",
     "webhook_secret": "<secret result, supplied privately through stdin>",
     "instructions": "Classify the request and prepare a reply in Russian.",
     "mode": "draft",
     "channel_ids": []
   }
   ```

   An empty channel list means all channels in this project. Other business
   conditions belong in the operator instruction. The helper uses Hermes' native
   atomic subscription lock, checks ownership/static route conflicts and fixes
   the project filter. Dynamic routes hot-reload. It saves no OAuth credentials.
   The helper embeds the package's incoming-event instruction directly in the
   prompt: Hermes' webhook skill loader does not reliably resolve plugin skills.
   The background run needs only the User MCP toolset. Project, dialog and event
   IDs are rendered separately from the possibly truncated JSON body.
4. For **each selected event**, call MCP test with that `event_type`, then pass
   the complete result object to `record-test` as `result`. The helper requires
   HTTP 202, `status: "accepted"`, and matching route, event and delivery ID.
   `success: true` alone, HTTP 200, `ignored`, `duplicate`, truncated results or a
   network error do not pass. Do not replace the server result with fabricated
   data. Tests may run the Hermes model; their skill forbids MCP calls and sends.
   Reuse a result only to retry recording that same test; this does not renew
   its expiry. Always call Senler test again after reconfiguration. The helper
   remembers a bounded history of used receipts to reject accidental reuse.
5. Re-read the Senler webhook. Pass it as `webhook` to `ready`. Within ten minutes
   of the successful tests, **immediately update its status to active via MCP**;
   enabling is part of the operator's connect request. Pass the successful update
   result to `mark-active`. If the write times out, read the current status before
   retrying. If a test fails, leave the subscription disabled and report why.
   If remote activation succeeded but `mark-active` rejects the local state,
   disable this same owned webhook through MCP and report the mismatch; do not
   leave a connection active after a failed final verification.
6. Report the project, events, channel restriction, draft/send mode and actual
   active status. State that the test verified signed event acceptance, and that
   a real-event check is still needed for the model's business behavior. Never
   claim that HTTP 202 proves the job completed.
   Give the user a way to see the results: ask Hermes to show this workflow's
   drafts/results from session history. The helper returns its search marker.
   `deliver: log` writes only a short preview and does not deliver a message to
   the setup chat or save a draft in Senler.

## Existing connections

- **Find:** use `connections` with `project_id`, or `{}` when only local discovery
  is possible. It returns only this plugin's routes and no signing keys. Follow
  `next_cursor` as `after` to read the next bounded page. The returned phase is
  saved local state, not proof of the live Senler status.
- **Status:** `status` reports saved local state only. Read Senler status through
  MCP and run `preflight` for live gateway/OAuth checks.
- **Change/resume:** repeat disabled → configure → test → automatic activation.
  Reconfiguration invalidates old test receipts and reuses the same webhook ID.
  Repeat this after a plugin update to embed the new incoming instruction.
- **Pause:** first call `pause` without a `webhook` object to stop the local route
  immediately. Then disable that exact webhook through MCP and call `pause`
  again with its response as `webhook`. If OAuth or Senler is unavailable, report
  `paused_locally`: Hermes rejects new deliveries, but stopping delivery from
  Senler is not yet confirmed. Do not ask the user to restore the public URL in
  order to pause. A later remote confirmation checks the same project and ID;
  edits to the webhook's URL/events do not prevent stopping it.
  A job already accepted by Hermes may still be running; pausing is not a
  cancellation of that job.
- **Disconnect:** delete only the owned webhook through MCP; after confirmed
  deletion, call `remove` with its `webhook_id`. If deletion cannot be confirmed,
  pause locally and retain the binding for recovery. Preserve other routes and MCP.

Hermes' delivery-ID cache is in memory. It does not guarantee that an event
accepted just before a crash will finish, or serialize separate events in one
dialog. Use server idempotency for replies and check for a newer operator reply;
do not promise exactly-once processing or general durable background execution.
