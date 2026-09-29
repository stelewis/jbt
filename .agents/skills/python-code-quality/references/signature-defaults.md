# Signature Defaults

Remove defaults that hide dependencies, mask missing inputs, share mutable state, or silently change core semantics.

## Find

- constructed dependencies such as `client=Client()` or `now=datetime.now()`
- mutable defaults such as `items=[]`
- `None` defaults that become implicit dependencies or critical values inside the function
- empty strings, zeroes, or arbitrary timeout and limit defaults in core code

## Refactor

1. Classify the value as a dependency, mutable collection, optional input, or policy default.
2. Make required core inputs required.
3. Construct dependencies at a composition root.
4. Put policy defaults in configuration or boundary code.
5. Use `None` plus local construction only for a genuinely optional fresh value, not to conceal a dependency.
6. Update all callers and tests; remove legacy overloads.

Defaults are appropriate at user-facing boundaries when they are an explicit part of that boundary's contract.
