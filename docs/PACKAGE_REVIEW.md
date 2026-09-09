# Package and assistant review

Reviewed the working tree on 2026-09-09. This covers request validation,
claims, plan approval/execution, the built-in assistant and agent MCP,
SSH command construction, and the new HTTP controls. All verification used
dry-run, synthetic inputs, or mocked transports; no robot or service was changed.

## Confirmed defects fixed

| Priority | Problem | Change |
|---|---|---|
| High | A different authenticated owner could reuse a public session ID and receive the current owner's claim token. | Reclaims require both owner and session to match. Claim mutations are serialized. |
| High | Calling release without a token cleared another caller's claim. | A missing or wrong token cannot release a live claim; explicit internal shutdown still has its own clear operation. |
| High | Older liquid endpoints accepted commands in error, paused, or external-control states, despite withholding them from `allowed_actions`. | The shared command runner checks the same advertised action gate before invoking the transport. |
| High | Aborting a plan changed its record while the executor continued submitting later steps. | Abort prevents subsequent steps and preserves the actual result of a command already started. It does not itself interrupt motion. |
| High | A plan could continue after the approving operator's claim expired or changed owner. | Recheck live owner, session, claim expiry and approval expiry before each step. |
| High | Approval validation and execution reservation could race with another execute or revise request. | Protect plan transitions with a lock; reserve execution atomically. Aborted plans with a command still in flight cannot be deleted. |
| High | SSH command construction interpolated unvalidated reference names and unquoted setup strings into Python source. | Reject Python expressions in gateway references and well addresses; validate setup aliases; quote setup data as Python literals. Custom definition load names are no longer Python assignment targets. |
| Medium | Anyone could use paid chat while somebody else held the robot claim. | Both chat endpoints validate the caller's token and recheck it between model/tool rounds. The UI already supplies this token. |
| Medium | Propose-only credentials could reach controls when cooperative claim enforcement was disabled. | Keep their permission restriction even in that configuration. |
| Medium | Assistant guidance assumed OT-2 fixed trash on Flex and described blow-out as unavailable. | Use the configured robot profile, correct the tool descriptions and stop boundary, and document direct motion. Require explicit observations for physical-state corrections. |
| Medium | An OpenAI key could be sent to the default OpenRouter URL; a file key could override an environment key under another supported name. | Require an explicit provider URL with `OPENAI_API_KEY`; apply environment-first key selection across both names. |
| Medium | Malformed assistant numeric settings broke its health endpoint; SDK clients were not explicitly closed. | Report chat as unavailable with a configuration diagnostic, and close the client after completion, exceptions, or stream cancellation. |
| Medium | Provider exception text was returned directly to chat clients and logs. | Return the exception category without reflecting provider bodies that may contain credentials or request details. |
| Low | Executing an unknown plan returned an internal error; concurrent revision could invalidate the approval audit object. | Return 404 for missing plans and take an approval snapshot before emitting the audit record. |

The assistant still has only read tools and `propose_plan`. It cannot acquire
a claim, approve a proposal, execute a plan, issue a stop, run Python, or open
a shell. Fixing prompt wording does not grant any additional tool capability.

## Remaining issues and verification limits

- The legacy SSH `OT2Control.move_labware()` delegates to its gripper method,
  which passes `use_gripper=True`. This is invalid on OT-2. Simply switching
  that flag is not equivalent to the HTTP operation: the public Python manual
  move pauses for operator confirmation, whereas this gateway's HTTP move
  records an operator-performed relocation without a pause. The SSH manual
  confirmation/recovery workflow still needs implementation; its existing
  behavior has not been changed as part of these fixes. See the
  [official movement guide](https://docs.opentrons.com/python-api/moving-labware/).
- The development configuration permits cooperative claims without verified
  login. The owner check does not turn that mode into authentication. Actual
  deployment auth settings were not inspected or changed in this review.
- `AGENT_RULES.md` still points to the retired canonical
  `ac-organic-lab/docs/AGENT_RULES.md`. The replacement is Part I of
  `ac-organic-lab/docs/AGENTIC_LAB_DESIGN.md`, as documented by the canonical
  repository. No binding rules were edited.
- Flex hardware acceptance, 96-channel/partial layouts and migration of the
  separate sample-prep workflow remain outside the implemented surface. See
  [Flex support](FLEX_HTTP_SUPPORT.md).
- No live model-provider call or physical motion was used to verify these
  fixes. The assistant tests replay model responses, including tool failures
  and lost claims. General model answer quality is not established by them.

The regressions are covered by `tests/unit/test_review_regressions.py`, with
additional assistant, SSH-formatting and plan tests in their existing suites.

Final verification: **624 unit tests passed**, UI TypeScript checking passed,
and `git diff --check` passed. Windows pytest emitted a temporary-directory
cleanup permission warning after the successful suite; the process exited 0.
