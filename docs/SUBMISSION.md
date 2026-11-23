# Fe Builders Challenge: submission drafts

## Pitch (95 words)

Fe already has fixed-point types in `core::fixed`. Fathom covers what DeFi
code still needs on top of them. Rounding modes are types (`x.mul_r<Up>(y)`),
so the direction is visible at every call site and checked by the compiler,
for signed and unsigned values. Checked operations return `Option`. `exp`, `ln`, `pow`, `log2` and `log10` work at any
scale up to 36 decimals, including Ray. A differential harness measured core
and Fathom against a 110-digit reference: 27,000 samples, every rounding
contract held exactly, and Fathom's `pow` is about 100 times more accurate.
Includes tested savings-vault and AMM demos.

## Fe-specific highlight

Rounding modes are zero-sized types implementing a `Rounding` trait, and the
scale is a const generic. Together they give `fn mul_r<R: Rounding>(self, other: Self) -> Self`,
written once in `impl<const D: u256> FixedOps for UFixed<D>` and once for
`Fixed<D>`. The compiler creates a separate instance for each mode and scale,
so there is no runtime rounding flag, `Wad` and `Ray` cannot be mixed, and a
helper such as `fn fee_up<T: FixedOps>(amount: T, rate: T) -> T` serves
`Wad`, `Ray`, `SWad` and `UFixed<6>` alike. `exp<const D>` / `ln<const D>`
are likewise single functions that give Ray-precision transcendentals,
which core provides only at 18 decimals. Fathom attaches these methods to
core's own types through an extension trait, so existing `Wad` code adopts
it without wrapping or conversion.

## Declaration of pre-existing work, reused code and AI assistance (draft)

* **Pre-existing work:** none. The repository was created during the contest
  period (first commit 2026-11-15).
* **Reused code / dependencies:** Fathom depends on Fe's standard library
  (`core::fixed`, `core::num`, `std::evm`; argotorg/fe, Apache-2.0). It calls
  `checked_mul_div` / `mulmod` (Remco Bloemen's mul-div algorithm, as in
  Uniswap FullMath) and `UFixed`'s `**` and `sqrt`. It does not copy them.
  The harness measures core's ports of Solady's `expWad` / `lnWad` /
  `powWad` (Vectorized/Solady, MIT) but does not reuse their code. All other
  code is new. The mathematical methods (Taylor and atanh series, Newton's
  method, scaled-balance accounting, constant-product formulas) are standard
  techniques.
* **AI assistance:** This project was built with substantial AI assistance
  (Anthropic's Claude, working as a coding agent in Claude Code) under the
  entrant's direction. The entrant chose the project and its scope, and
  redirected it after the agent found that `core::fixed` already covered the
  original plan. The AI wrote most of the code, tests, harness and
  documentation, and the entrant reviewed them. Every accuracy figure was
  produced by running the code (see `harness/run.py`), not estimated.

  > Entrant: edit this paragraph so it accurately states how much you
  > reviewed or changed yourself before you submit.

## Private submission form checklist (fill in yourself)

- [ ] **Repository URL:** publish this repo (e.g. GitHub, public) and paste the URL. Replace `https://github.com/drakep/fathom` in `docs/FINDINGS.md`.
- [ ] **Commit SHA:** after your final commit, `git rev-parse HEAD`.
- [ ] **Fe version:** `26.6.0` (`fe --version` prints `fe 26.6.0 (5b3f96a)`). It is pinned in `FE_VERSION` and installed by `scripts/install-fe.sh` with sha256 verification.
- [ ] **License:** MIT (`LICENSE`). Optionally replace "The Fathom authors" with your name.
- [ ] **Build/test instructions:** README, section "Build and test". Command: `scripts/test.sh` (expect `test result: ok. 42 passed; 0 failed`).
- [ ] **Demo:** `examples/savings` and `examples/amm`, run by `fe test .`.
- [ ] **Pitch, Fe-specific highlight, declaration:** copy from above. Review the AI paragraph.
- [ ] **Optional:** file the draft upstream issues in `docs/FINDINGS.md` (not filed).
- [ ] Re-run `scripts/test.sh --harness` if you change any math, and commit the regenerated `docs/ERROR_BOUNDS.md`.
