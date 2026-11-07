# Required reading: native Northflank push-to-deploy

Read this file before changing, testing, reviewing or deploying this repository.
This is the current design and operating instruction. It supersedes older CI,
runner and deployment guidance in this repository where they conflict. Historical
records below their notices remain evidence, not execution instructions.

## Current design

- Native Northflank slots use buildpacks connected to the intended source branch.
  Pushes to that branch trigger the native buildpack build/deploy flow. Test,
  staging and production each follow their own intended branch; production uses
  the deliberate qualified main push. Do not replace this with container-image
  deployment, an image registry, or a manual build/image-selection workflow.
- Configure the slot's native source branch, buildpack context and custom entry
  through its supported settings/API. Do not call a manual image-selection error
  a prerequisite for branch-push deployment. This document specifies the route;
  it does not prove those settings have already been applied or a runtime passed.

- Use native Northflank test/staging applications on existing free, reusable
  slots. These are normal applications with the exact reviewed source and custom
  entry/command configured through the API, not GitHub Actions runners.
- A GitHub-hosted or self-hosted Actions runner, runner registration, runner-group
  lookup, Actions queue or Actions minutes is not a prerequisite for native
  qualification. Do not repair or wait for that old route to run native tests.
- Before main, run the required tests, scans and realistic checks against the
  reviewed candidate. Keep its exact source/tree, inputs, tool versions, results
  and logs bound together. A build, HTTP success, queued job or source review is
  not a passing test. An unrun check stays UNRUN.
- Isolate each test/staging branch's database, secrets, variables and settings.
  Use a volume only within existing available capacity, then empty it and return
  it to the pool. Do not reuse a live session or another workload's private state.
- Coordinate the free slot before use. Collect actual results and logs, clean
  owned temporary data, and verify release before marking the slot free. Do not
  interrupt an occupied slot or silently reassign its resources.
- Production is triggered only by a deliberate new push to main by the owner or
  the authorized operator. That push deploys already-qualified source directly.
  Do not repeat qualifying tests/scans on main or invent a separate automatic
  production-blocking or promotion gate. Review branches remain the safe route
  for preparing changes; this instruction does not authorize an unreviewed main
  push or bypass existing protections.
- Preserve the link between the qualified candidate and the deployed main
  content. A new merge/squash SHA is not proof that older results cover different
  bytes. Verify source/tree and artifact identity without rerunning main tests.
- Use existing free resources only. No new spending, home-device testing, Jules,
  or unapproved provider, credential, secret or workflow changes follow from this
  documentation. A historical service/repository name containing "runner" is an
  identifier, not a requirement to use Actions runners.

## Implementation status is separate from the design

The documentation does not assert that a native branch-push buildpack flow, scanner,
release receiver or production route is already working. Verify current source,
configuration and runtime evidence before execution. Report the concrete missing
native capability, input, branch/buildpack configuration or receipt/admission join, not "waiting
for runners". Legacy Actions jobs may still appear because workflows have not
been migrated; they are not native qualification receipts.

Do not cancel jobs, disable workflows, change required checks, broaden credentials
or replace production authentication merely to make this policy appear true.
Those are separate implementation changes requiring their own scope and review.
Keep historical incident and approval records intact; their contents do not
create current authority or prove current availability.

## Observed repository migration gaps

At the inspected source base `cecd8b78f8e1e732b8b5dc2f77bb6e23b3f6b49c`, these workflow files still describe Actions-based execution:

- `.github/workflows/pulse.yml`
- `.github/workflows/test.yml`

These are observed source-level migration gaps, not proof of present runner availability or a required native test route. No workflow was changed by this documentation update. Native runtime readiness and production wiring must be verified separately.
