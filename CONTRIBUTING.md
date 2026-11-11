# Contributing Guidelines

Thank you for your interest in contributing to this project. We welcome
contributions from the community to make this guidance better for
everyone.

This document outlines the process and conventions for contributing.

## Code of Conduct

This project has adopted the
[Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the
[Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or
contact opensource-codeofconduct@amazon.com with any additional
questions or comments.

## Reporting Bugs / Feature Requests

We welcome you to use the GitHub Issue tracker to report bugs or suggest
features.

When filing an issue, please check
[existing open issues](https://github.com/aws-solutions-library-samples/guidance-for-agentic-vehicle-experience-on-aws/issues)
and [recently closed issues](https://github.com/aws-solutions-library-samples/guidance-for-agentic-vehicle-experience-on-aws/issues?utf8=%E2%9C%93&q=is%3Aissue%20is%3Aclosed%20)
to make sure somebody else hasn't already reported the issue. Please try
to include as much information as you can. Details like these are useful:

- A reproducible test case or series of steps
- The version of code being used
- Any modifications you've made relevant to the bug
- Anything unusual about your environment or deployment

## Contributing via Pull Requests

Contributions via pull requests are much appreciated. Before sending us a pull request, please ensure that:

1. You are working against the latest source on the *main* branch.
2. You check existing open, and recently merged, pull requests to make sure someone else hasn't addressed the problem already.
3. You open an issue to discuss any significant work - we would hate for your time to be wasted.

To send us a pull request, please:

1. Fork the repository.
2. Modify the source; please focus on the specific change you are contributing.
3. Ensure local tests pass.
4. Commit to your fork using clear commit messages.
5. Send us a pull request, answering any default questions in the pull request interface.
6. Pay attention to any automated CI failures reported in the pull request, and stay involved in the conversation.

GitHub provides additional documentation on [forking a repository](https://help.github.com/articles/fork-a-repo/) and [creating a pull request](https://help.github.com/articles/creating-a-pull-request/).

## Finding Contributions to Work On

Looking at the existing issues is a great way to find something to contribute on. Issues labelled 'help wanted' are a good starting point.

## Code of Conduct

This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct). For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact opensource-codeofconduct@amazon.com with any additional questions or comments.

## Security Issue Notifications

If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public GitHub issue.

## Agent Tool Inventory

When adding a new tool to the agent:

1. Edit the tool implementation under `agents/supervisor/tools/` and register it in the appropriate surface: `agents/supervisor/tools/__init__.py` for the six
   persona lists in `PERSONA_TOOL_FACTORIES`, and/or `agents/supervisor/supervisor.py` for the
   anonymous-kiosk allow-list `_KIOSK_TOOL_FACTORIES`.
2. Run `make agent-inventory` to regenerate the inventory artifacts:
   - `docs/AGENT-INVENTORY.md` — Markdown table of all registered tools
   - `docs/AGENT-INVENTORY.adoc` — AsciiDoc version for documentation packages
   - README.md fenced region — embedded summary for quick reference

3. Commit the regenerated files alongside your tool code.

**Do NOT hand-edit** `docs/AGENT-INVENTORY.md`, `docs/AGENT-INVENTORY.adoc`, or the generated region in `README.md` — they are produced by the generator. Hand edits will be overwritten and validation will fail.

**Pre-commit check:** Run `make agent-inventory-check` to verify the artifacts are in sync.
The same invariant is asserted by `agents/supervisor/tests/test_agent_inventory_drift.py`,
which runs under `pytest agents/`.

> **Not yet enforced in CI.** This repository runs no `pytest` job in any pipeline
> (`.github/workflows/lint.yml` covers yamllint, flake8 and shellcheck only), so the
> drift gate is currently developer-run. Wiring it in is tracked as a follow-on.

## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to confirm the licensing of your contribution.
