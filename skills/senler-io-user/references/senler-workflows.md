# Work with Senler through Hermes

Use this reference for the operator's interactive Senler task. Keep the parent
skill's native tool discovery, authorization and project scope. Discover current
MCP methods and schemas; names of widget tools or examples from another agent
are not capabilities of this Hermes session.

## Choose a solution for the requested outcome

- Respect the user's explicit choice of agent, automation, API or external
  integration and any existing object being edited. Do not create a replacement
  simply because another approach is familiar.
- For conversational help, consider an agent. For a fixed sequence of messages
  or actions triggered by an event or condition, consider an automation. Check
  the current documentation and capabilities against the actual requirement.
- An ambiguous word such as "bot" does not determine the entity type. Use the
  named object and conversation; if the choice remains material, explain the
  options briefly and ask one focused question.
- For a Senler task, first check whether its built-in features fit. Preserve an
  explicitly chosen external integration; explain a concrete limitation before
  proposing a replacement. Do not invent support for a service or protocol.

Give the steps for the selected approach. Keep essential conditions next to
examples: supported channel, connection type, authorization, required rights
and fields. One documented example does not establish every integration mode.

## Create and change agents or automations

1. Confirm the target project and read the existing object and draft, if present.
   Work on the referenced object and preserve unrelated settings, instructions,
   bindings and edits. Change only what the request authorizes.
2. Use the supported authoring workflow. Where changes belong in a draft, save
   there and inspect the saved result. Do not assemble a replacement instruction
   from a short search snippet or overwrite a whole field without reading it.
3. Creating an object alone does not authorize assigning every channel, enabling
   automatic replies, bulk processing, publishing, or sending messages. Apply
   these actions when the request already covers them; otherwise obtain the
   missing authorization before that step. Reuse approval for the same change.
4. Before publishing, read the current draft and check that it contains the
   agreed changes. If it also contains unrelated edits, explain what publication
   would include and resolve that scope. "Save" alone does not mean "publish".
5. Verify the saved or published result and give a useful next step. A draft or
   a missing live channel assignment does not by itself prevent testing: consult
   the documented test workflow instead of enabling production behavior merely
   to make a test possible.

Run a paid test, create test data or send a test message only when covered by
the user's request. A request to configure Relay notifications follows its own
documented connection and delivery checks in [Relay setup](hermes-relay-setup.md).
Creating or publishing a solution alone does not authorize an additional test.
When asked to test, check the intended steps and final data; a `completed` label
or the first outgoing message does not prove the whole scenario works.

For copying existing resources, discover Senler's supported copy or resource
package export/import operations. Use that workflow to preserve relationships
and dependencies; do not reconstruct a copy with unrelated create calls. If
the supported method is unavailable, identify the missing capability.

## Investigate unexpected behavior

Start with the expected outcome, the observed result and the exact project,
object, dialog, event or run. Read only the related records. Account for the
version and settings in effect when that result was produced: a historical
failure does not prove a newly installed fix failed.

Find the stage where behavior diverged: saved configuration, publication,
channel or trigger, permissions, execution, model/tool result, or delivery.
Check evidence against the suspected cause, including contradictory evidence.
Do not repeatedly reconfigure, reauthorize, restart or replay an event merely
to retrieve its result. Honor an explicit no-replay/no-change request.

For an agent answer, distinguish:

- a wording-only edit to the proposed reply;
- a defect or ambiguity in the target agent's instruction;
- missing or incorrect knowledge content;
- a failure to retrieve or use relevant information;
- a tool, permission or delivery failure.

Use the agent ID associated with the answer. The assistant helping the operator
is not necessarily that agent. Read the relevant instruction, knowledge and
available execution evidence before assigning a cause. If the user asks for
better behavior in future, propose or apply the authorized instruction/knowledge
change as well as a corrected reply when useful. A request to rewrite one reply
does not authorize editing the agent or sending that reply to the customer.

Add only verified information to project knowledge, on the user's request.
Keep explanations in the operator's language and a proposed customer reply in
the customer's language. Identify which artifact was changed and whether the
change is a draft, published, or still untested.

For audio issues, distinguish microphone recording, keyboard dictation and
recognition of an already sent file. Read the relevant testing/media docs; an
agent's recognition setting does not establish the state of a browser microphone.
Do not run a paid media test just to explain the setting.

## Tables, access conditions and recipients

For structured project data, check Senler's built-in tables and their current
MCP support before proposing a new external spreadsheet integration. Select the
store that fits the task; keep an existing or explicitly requested integration
unless a verified limitation requires a change.

Before configuring an access check, determine whether the condition is Senler
segment membership, membership in a channel's community, permission to receive
messages, or paid access. These conditions are distinct. A "check again" button
does not enroll a person or prove payment. Use the selected segment's returned
subscription link where appropriate; paid access requires a verified grant and
expiry workflow. Do not substitute a public subscription for payment checks.

Recipients and IDs belong to a specific project and channel. A customer, an
operator receiving a notification, a Senler account and a platform account are
different targets. Never reuse a VK ID as a Telegram or Senler user ID. For
project invitations, verify the documented account identifier and choose a role
that fits the request instead of defaulting to administrator.

For notifications built inside a Senler automation, check its supported message
step, connected channel and authorized recipient. Choose variables for that
recipient and the original customer deliberately. For this plugin's background
monitoring, use [Relay setup](hermes-relay-setup.md): resolve the operator's native
Hermes destination, save the policy and verify the exact delivery test. Do not
silently switch a Relay notification to a different messaging path.

## Help through Hermes's current interface

Hermes is not the Cabinet widget. Do not assume access to its page-selection
context, `page_element_action`, `set_message_buttons`, or widget custom actions.
A link identifies a resource; it does not show what another browser or device
currently displays or which account is signed in there.

Use links returned by MCP or documentation to open the relevant object or
article. If no verified URL is available, give its name and the documented menu
path. Use precise menu labels and simple Markdown. Do not fabricate screenshots,
relative image URLs, callback buttons or navigation payloads. If the current
Hermes session offers an appropriate UI tool, inspect its actual schema and
confirm execution before saying an element was opened or highlighted.

When a manual step such as browser sign-in remains, explain just the next step
and expected result. Native Senler sign-in still follows the parent skill's
Hermes authorization card; do not substitute shell/browser automation for it.

For a requested image, use a suitable available generation tool or an image the
user supplied. Use its returned URL or ID when attaching it to a Senler resource,
and verify the saved result. If that surface cannot accept the output or generation
failed, state the unresolved step instead of inventing an asset URL.

## Report evidence and remaining work

Lead with the result or direct answer, then the detail needed for the next step.
Distinguish a proposal, a saved draft, a published change, a verified test and
confirmed live operation. Saving settings or receiving an event is not proof of
successful processing or delivery. State what is still unverified.

On failure, identify the failed stage and the next available action. If the
evidence requires service support, collect only the relevant project/object ID,
time and sanitized error; do not request tokens, passwords or unrelated customer
data. If a previous explanation was wrong, correct it and continue the original
task. Do not promise ongoing observation without a configured event workflow.
