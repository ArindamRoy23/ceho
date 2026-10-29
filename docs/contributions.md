# Contributing to CEHO

Work is tracked on the [CEHO GitHub project](https://github.com/users/ArindamRoy23/projects/1). Requirements live in GitHub issues. The docs in this folder are the product manual: functional pages for what the product does, and technical pages for how it is built. Every pull request leaves that manual accurate for the change it makes.

`docs/CEHO_PRD.md` is the vision. It is not the backlog. A behavior is scheduled only when it has an issue on a milestone. Feature work leaves the vision document unchanged unless the vision itself changed.

## Main stays closed

All development happens on a branch cut from `main`. Every change reaches `main` through a pull request, including when one person is doing the work. That person may open the pull request and merge it. The pull request is the checkpoint that the issue is linked and the manual is updated.

One branch is one pull request.

## Release slices and milestones

A release slice is the set of behaviors meant to finish together, so the product can do one new thing from end to end. A slice is small enough to describe in a few sentences and demo.

A milestone is the GitHub milestone for that slice. One slice, one milestone, and the milestone carries the slice’s name. Issues for the slice are assigned to it.

The vision describes a large product goal. A release slice is a cut of that goal, not the whole document.

## Requirements are issues

The unit of work is one behavior a person could observe. Track it as one issue on the project, assigned to the milestone for its slice.

The issue states:

- the behavior
- the acceptance criteria
- what is out of scope

Acceptance criteria stay in the issue. They are the contract a review checks. They are not copied into the manual.

An issue is ready to build when those three parts are written and the milestone is set. Close the issue when the acceptance criteria are fully met. A later change to that behavior is a new issue. The closed issue remains the record of what was promised and accepted.

Open a separate technical issue only when one design will be built across several pull requests. Otherwise the technical page updated on the branch is the design record.

### Spikes

A spike is an issue whose result is a decision. Write the time box on the issue. The pull request records the decision on a technical page. A spike does not change user-facing behavior and does not add a functional page.

## The product manual

This folder is the manual that gets published. It has two audiences. It has no requirements section. Open requirements stay in issues until the product matches them.

### Functional

`docs/functional/` describes what a person can observe and how to do it: concepts and guides.

Add or update a functional page in the pull request that makes the behavior true, when the issue is met. The page explains the behavior for a reader. It describes only behavior the product has.

### Technical

`docs/technical/` describes how that behavior is built: design, contracts, and decisions, grouped by capability.

Keep all implementation detail in
`docs/technical/architecture/low-level-design.md`. Do not add companion
architecture pages for schema or process slices; split later only when asked.
`local-development.md` stays limited to the local environment.

A technical page can land before any functional page, because a contract or a decision is needed before users see the behavior. Cite the issue the page satisfies. Leave the user-facing promise in the issue and, once met, in the functional page.

```text
docs/
  CEHO_PRD.md
  contributions.md
  functional/
    concepts/
    guides/
  technical/
    architecture/
    <capability>/
      design.md
      decisions/
    contracts/
    evaluation/
```

Create a capability directory when a slice starts work there. Do not add directories for vision areas that are not in the current milestone.

## Pull requests

Name the branch `<issue-number>-<short-name>`.

Link the issue in the pull request:

- `Closes #<n>` when this pull request fully meets the acceptance criteria
- `Refs #<n>` when the issue stays open

Declare the documentation class in the description:

- **Functional.** The observable behavior changed. Link the issue and update the functional page.
- **Technical.** The approach changed and the observable behavior did not. Link the issue and update the technical page.
- **None.** Refactor, dependencies, CI, or tests only. Write `Docs: none` and why the behavior and the design are unchanged.

A change to both the behavior and the approach updates both sections and says so.

`Docs: none` does not cover a change to behavior or design. A documentation edit that exists only to touch this folder does not count as an update.
