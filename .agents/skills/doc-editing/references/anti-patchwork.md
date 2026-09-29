# Anti-Patchwork Documentation

Agentic maintenance often produces individually reasonable edits that collectively degrade a documentation set. A code change matches several pages, so each page receives another sentence, warning, exception, or cross-link. The facts remain technically correct while the prose loses structure, ownership, and confidence.

The remedy is composition, not smaller patches.

## Warning signs

- a page explains its own editing history with phrases such as "now," "new," "currently," or "as of"
- new paragraphs qualify older paragraphs instead of replacing them
- warnings and notes repeat the same constraint in several places
- one concept has complete explanations in multiple documents
- headings reflect implementation milestones rather than reader questions
- a short workflow is interrupted by exceptions, defensive disclaimers, and internal details
- pages grow after every change but are rarely shortened or reorganized
- links are added wherever a term appears instead of where a reader needs a handoff

## Rewrite procedure

1. Read the entire page and identify the single reader question it should answer.
2. Extract the durable facts that are still true after the change.
3. Remove superseded claims, transitional language, duplicated cautions, and details owned elsewhere.
4. Sketch the smallest outline that would explain the current system from scratch.
5. Rewrite the affected section against that outline. When the old structure no longer fits, rewrite the page rather than preserving sentence-level history.
6. Search related docs for duplicate ownership. Keep the full explanation in one canonical page and retain only reader-specific consequences elsewhere.
7. Reread the result without knowledge of the change. It should sound like a finished page, not a sequence of corrections.

## Patch or rewrite

A local patch is appropriate when the page structure and surrounding explanation remain correct, such as fixing a command, value, or isolated factual statement.

Rewrite a section or page when:

- the new behavior invalidates the framing of existing prose
- the edit needs words such as "however," "except," or "previously" to coexist with stale text
- the same qualification would be added to multiple paragraphs or files
- ownership of the concept has moved
- the patch would make the page longer while leaving obsolete explanation in place

Diff size is not the objective. Prefer the smallest coherent final document, even when reaching it requires replacing more lines.

## Scope control

Do not turn every code change into a repository-wide docs rewrite. Expand scope only to:

- the canonical page that owns the changed contract
- task-oriented pages whose instructions are now wrong
- duplicates that would contradict or obscure the canonical explanation

Leave merely related pages alone when their reader contract has not changed.
