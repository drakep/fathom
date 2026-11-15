# Findings: core::fixed under differential testing

Toolchain: `fe 26.6.0 (5b3f96a)`. Method: `harness/run.py` evaluates each
function on the `fe test` EVM backend and compares the result with a
110-digit Python `decimal` reference, or with an exact rational where the
function promises a rounding direction. Raw numbers are in
[ERROR_BOUNDS.md](ERROR_BOUNDS.md).

## Summary

**No correctness bugs were found in `core::fixed`.** Every function with a
stated rounding contract met it on all 1,000 samples (0 violations):
`sqrt` (floor, including radicands above 2^256), `Ray *` (floor), `SWad *`
and `SWad /` (towards zero). The Solady ports behave as documented: `exp` is
within 1 ulp (relative error 9.9e-19), and `ln` is within 1.04 ulp.

The observations below describe accuracy and API limits, not defects.

## Observations

### O1. `Wad::powf` error grows with |y ln x|; measured relative error 8.3e-17

`powf(x, y) = exp(ln(x) * y)` evaluates the product at 18 decimals, so the
roughly 1 ulp error in `ln` is multiplied by `|y|` before `exp`. The worst
sampled case was x = 1.039741430760137460, y = 99.456620543106502826:
core = 48.232312559930292465, exact = 48.232312559930296475… (relative
error 8.3e-17). Fathom's `pow<18>` keeps `y ln x` at 1e-36 and measured a
maximum relative error of 6.7e-19 on the same sample set.

### O2. `UFixed ** n` loses absolute precision for fractional bases

Rounding half up at each squaring step adds error at every step. For results
below 1.0 the harness measured up to 2,990 ulp absolute error. A pinned
reproduction is in `ingots/fathom/src/test_math.fe` (`integer_power_accuracy_vs_core`):

    Wad::from_raw(999671360967728412) ** 1557
      core   = 0.599430601118269349
      exact  = 0.599430601118269642734...
      Fathom pow<18>(x, 1557) = 0.599430601118269642 (or ...641)

The relative error stays small (at most 3.7e-16 measured), and this is the
known behaviour of MakerDAO/Solady `rpow`. Applications that compound
fractional factors over many periods should know about it.

### O3. Signed and unsigned default rounding differ

Unsigned `*` and `/` floor (round towards -inf). Signed `*` and `/` truncate
(round towards zero). Both behaviours are documented, but `-1/3` and `1/3`
therefore round in opposite directions on the number line, and `Fixed` has no
`mul_up` / `div_up` (no floor or ceiling) variants.

### O4. Transcendental functions exist only at 18 decimals

`exp`, `ln` and `powf` are defined on `Fixed<18>` / `UFixed<18>` only, which
is deliberate according to the module docs. Ray-based protocols (Aave- or
Maker-style rate math) must rescale to Wad and lose 9 digits.

### O5. `powf(0, y)` fails for every y

`powf` calls `ln(0)`, which fails `assert!` (`Panic(0x01)`). Mathematically
0^y is 0 for y > 0 and 1 for y = 0.

## Draft upstream issues (NOT filed)

Drafts for the entrant to file at their discretion. Replace `<repo URL>`.

---

**Title:** core::fixed: document the accuracy of `Wad::powf` and `UFixed ** n`

> Differential testing against a 110-digit reference (Fe 26.6.0, 1,000
> samples each) measured:
> - `Wad::powf`: max relative error 8.3e-17 (e.g. `powf(1.039741430760137460,
>   99.456620543106502826)` is 4.0e-15 too low), because `ln(x) * y` is
>   formed at 18 decimals.
> - `Wad ** n`: up to about 3,000 ulp absolute error for results < 1, e.g.
>   `Wad::from_raw(999671360967728412) ** 1557 == 0.599430601118269349` vs
>   exact `0.5994306011182696427`.
>
> Neither is a bug, since both match Solady, but the docs give no bound.
> Suggest stating the bounds, or forming `ln(x) * y` with extra precision
> inside `powf`. Reproduction: <repo URL>/harness/run.py

---

**Title:** core::fixed::Fixed: add directed rounding (floor/ceil variants or a rounding parameter)

> `UFixed` has `mul_up` / `div_up`, but `Fixed` only truncates. Protocols with
> signed quantities (PnL, funding rates) need floor and ceiling to round
> against the user consistently. A library implementation
> (`signed_mul_div_r<R: Rounding>`) with exact-contract tests is in
> <repo URL>/ingots/fathom/src/rounding.fe.

---

**Title:** `Wad::powf(0, y)` panics for y >= 0

> `powf` computes `exp(ln(x) * y)`, and `ln(0)` fails `assert!`. Suggest
> returning `one()` for `y == 0` and `zero()` for `y > 0`, and documenting that
> negative `y` fails.
