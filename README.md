# Fathom

**Rounding-aware, checked and measured fixed-point math for Fe.**

Fathom is a library built on top of Fe's standard `core::fixed`. It does not
replace it. Since Fe 26.x, `core::fixed` provides `UFixed<D>` / `Fixed<D>`
(with `Wad`, `Ray` and `SWad` aliases), full-precision `*` and `/`,
`mul_up` / `div_up`, `sqrt`, integer `**`, and bit-exact ports of Solady's
`expWad` / `lnWad` / `powWad`. Fathom adds what protocol code still needs:

| | `core::fixed` (Fe 26.6.0) | Fathom adds |
|---|---|---|
| Rounding | unsigned: down, plus `_up` methods; signed: towards zero only | **rounding modes as types**: `Down` / `Up` / `Zero` / `Nearest`, the same for signed and unsigned values (`x.mul_r<Up>(y)`) |
| Failure | panics (`Panic(0x11)`, `Panic(0x12)`) | **`Option`-returning** `checked_mul/div/add/sub`, `checked_rescale` |
| Transcendentals | `exp` / `ln` / `powf` at **18 decimals only** | `exp` / `ln` / `pow` / `log2` / `log10` for **any scale `D <= 36`** (Ray included), evaluated at 1e-36 internally |
| Roots | `sqrt` | exact `cbrt` |
| Accuracy | documented as Solady-equivalent | **measured**: a differential harness compares core and Fathom with a 110-digit reference ([docs/ERROR_BOUNDS.md](docs/ERROR_BOUNDS.md)) |
| Domain helpers | none | interest rates (`compound`, `apr_to_per_second`, `Accumulator`, scaled balances) and constant-product AMM math, all with explicit rounding |

Two tested demo contracts use the library: an interest-accruing savings
ledger and a constant-product pool.

## Quick look

```fe
use core::fixed::{Ray, SWad, Wad}
use fathom::{FixedOps, Down, Up, Nearest, exp, ln, pow, SRay, to_scaled, from_scaled}

// The rounding direction is a type argument, resolved at compile time.
let fee  = amount.mul_r<Up>(rate)              // the protocol never under-charges
let paid = shares.mul_r<Down>(price)           // the protocol never over-pays
let px   = a.checked_div<Nearest>(b)           // Option<Wad>: no revert

// Signed values use the same modes: Down = floor, Up = ceiling.
let d = SWad::from_int(-1).div_r<Down>(SWad::from_int(3))   // -0.333...334

// Ray-precision transcendentals (core only has these at 18 decimals).
let growth: Ray = exp<27>(SRay::from_raw(50000000000000000000000000))  // e^0.05
let l: SRay = ln<27>(growth)

// Scaled balances: the rounding of each conversion is visible in the code.
let shares = to_scaled<Down>(deposit, index)
let owed   = from_scaled<Up>(debt_shares, index)
```

Code that is generic over the `FixedOps` trait works for every scale and both
signs:

```fe
fn fee_up<T: FixedOps>(_ amount: T, _ rate: T) -> T { amount.mul_r<Up>(rate) }
// fee_up(Wad, Wad), fee_up(Ray, Ray), fee_up(SWad, SWad), fee_up(UFixed<6>, ...)
```

## Fe-specific design

* **Rounding modes are zero-sized types** implementing a `Rounding` trait
  (`rounding.fe`). `mul_r<R: Rounding>` gets a separate instance for each
  mode, so there is no runtime flag and no branch on the mode. A misspelled
  or missing mode is a compile error, and every call site shows which way it
  rounds.
* **One implementation per operation, generic over scale.** `FixedOps` is
  implemented once for `UFixed<const D: u256>` and once for `Fixed<const D>`.
  Each implementation reads `Self::SCALE`, the `10^D` constant that core
  derives from the const parameter. `exp<const D>`, `ln<const D>` and so on
  are single functions that serve `Wad`, `Ray`, `UFixed<6>` and any other
  scale up to 36.
* **Mixing scales is a type error.** The functions take and return
  `UFixed<D>` / `Fixed<D>`, so core's guarantee that `Wad` and `Ray` cannot
  be mixed carries through Fathom's API. `checked_rescale<R, D, E>` makes
  every scale change explicit, with its rounding stated.
* **Extension traits on core types.** Fathom adds methods to
  `core::fixed::UFixed` through a trait instead of wrapping the types, so it
  works with existing `Wad` / `Ray` code without any conversions.
* **Effects in the demos.** The contracts declare `uses (ctx: Ctx)` and
  `uses (mut store)`, so the compiler checks which handlers can read the
  clock or write storage.

## Architecture

```
ingots/fathom/src/
  rounding.fe   Rounding trait; Down/Up/Zero/Nearest; mul_div_r, signed_mul_div_r
  ops.fe        FixedOps for UFixed<D>/Fixed<D>: mul_r/div_r, checked_*; checked_rescale
  math.fe       exp/ln/pow/log2/log10 (any D <= 36), cbrt; SRay alias
  rate.fe       compound, linear, compound_continuous, apr_to_per_second,
                to_scaled/from_scaled, Accumulator
  amm.fe        get_amount_out/in, initial/mint/burn_liquidity (generic over D)
  test_*.fe     unit, property-style and should-revert tests
examples/
  savings/      interest-accruing deposit ledger contract + tests
  amm/          constant-product pool contract + tests
harness/run.py  differential accuracy harness (Python stdlib only)
```

**How the math works.** `exp` splits the input as `x = k ln2 + r` with
`|r| <= ln2/2`, sums the Taylor series of `e^r` at 1e-36 until the terms are
zero, multiplies by `2^k` through core's 512-bit `mul_div`, and rounds down
once at the end. `ln` scales `x` to `m * 2^k` with `m` in `[1/sqrt2, sqrt2)`,
computes `ln m = 2 atanh((m-1)/(m+1))` (`|s| < 0.172`, so the series
converges quickly), adds `k ln2`, and rounds to nearest. `pow` keeps
`y ln x` at 1e-36 between the two steps. `cbrt` runs integer Newton
iteration from above on `raw * 10^(2D)`, with a nested 512-bit `mul_div` to
form `N / z^2`, and returns the exact floor. All signed/unsigned rounding
reduces to one primitive: given the quotient magnitude `q`, the remainder
`r`, the divisor `d` and the sign, each mode decides whether to add one.

## Measured accuracy (summary)

Measured with 1,000 samples per function (27,000 in total), edge cases plus
log-uniform random inputs, against a 110-digit reference. The full table is
in [docs/ERROR_BOUNDS.md](docs/ERROR_BOUNDS.md).

| function | max error, result < 1 | max relative error, result >= 1 | contract checked exactly |
|---|---:|---:|---|
| core `SWad::exp` | 0.999 ulp | 9.9e-19 | n/a |
| core `Wad::ln` | 1.02 ulp | 7.3e-19 | n/a |
| core `Wad::powf` | 3.4 ulp | **8.3e-17** | n/a |
| core `Wad ** n` | **2,990 ulp** | 3.7e-16 | n/a |
| core `sqrt`, `Ray *`, `SWad *`, `SWad /` | < 1 ulp | | floor / trunc, 0 violations |
| Fathom `mul_r` / `div_r` (all modes, signed and unsigned) | 0.5 ulp (Nearest), < 1 ulp (directed) | | 0 violations |
| Fathom `exp<18>` / `exp<27>` | 0.998 / 0.999 ulp | 9.8e-19 / 9.9e-28 | |
| Fathom `ln<18>` / `ln<27>` | 0.5 / 0.499 ulp | 3.7e-19 / 3.7e-28 | |
| Fathom `pow<18>` / `pow<27>` | 0.995 / 1 ulp | **6.7e-19** / 6.5e-28 | |
| Fathom `log2<18>`, `log10<27>` | 0.49 / 0.5 ulp | | |
| Fathom `cbrt<18>` / `cbrt<27>` | exact | | floor, 0 violations |
| Fathom `exp<36>` | 6.4 ulp | 1.0e-35 | (outside the guarantee) |

Fathom's error bound for `D <= 27`: at most 1 ulp for functions that round
down and at most 0.5 ulp (plus a negligible internal error) for those that
round to nearest, within the domain listed below. These figures were
measured on the sampled inputs. They are not a formal proof.

Core's figures are not bugs. `powf` amplifies `ln`'s error by `|y|`, as
documented, and `**` rounds at every squaring step. Details and draft
upstream notes are in [docs/FINDINGS.md](docs/FINDINGS.md).

## Build and test (reproducible)

Requires **Fe 26.6.0 exactly** (`FE_VERSION`). Python 3.10+ (standard library
only) is needed for the harness.

```sh
scripts/install-fe.sh             # downloads Fe 26.6.0 from github.com/argotorg/fe releases, checks sha256
export PATH="$HOME/.local/bin:$PATH"
scripts/test.sh                   # = fe test .   (42 tests across 3 ingots)
scripts/test.sh --harness         # also the differential harness (~4 min, 27k samples)
python3 harness/run.py --samples 200 --only fathom.Ray.exp,core.Wad.powf   # quick subset
```

`fe test .` runs every workspace member: the `fathom` library tests and both
demo contracts. The harness generates a throwaway ingot in
`harness/probe/src`, runs it with `fe test --show-logs`, decodes the results
from emitted events, and regenerates `docs/ERROR_BOUNDS.md`. It exits
non-zero if any rounding contract is violated or any probe reverts.

## Demos

* **`examples/savings`**: depositors receive `shares = amount / index`,
  rounded down, and withdraw `shares * index`, also rounded down. The index
  compounds a per-second Ray rate obtained from an APR with
  `apr_to_per_second`. The tests check deployment, round trips, and that
  twenty 1-wei deposits cannot withdraw more than they put in. The test EVM
  has no time-warp cheat, so accrual over time is tested on
  `fathom::Accumulator` directly (`test_rate.fe`).
* **`examples/amm`**: a 0.3%-fee x·y=k pool. The tests check the geometric-mean
  initial liquidity, a swap against a reference value computed in Python,
  that `k` never decreases over 25 swaps of varied sizes, that
  `quote_in` covers `quote_out`, that burning liquidity never returns more
  than the burned share, and the slippage revert.

Both demos are accounting ledgers. They do not transfer tokens.

## Domains and limitations

* `exp<D>`: returns 0 below one ulp and panics with `Panic(0x11)` when the
  result exceeds `u256` (about x > 115.27 for Ray, 135.3 for Wad).
  `ln` / `log2` / `log10` fail for 0. `pow(0, y)` is 1 for `y = 0`, 0 for
  `y > 0`, and fails for `y < 0`.
* Scales above 27 decimals lose accuracy, because the internal precision is
  only 36 digits (measured 6.4 ulp at D=36). `D > 36` is rejected at run time
  with an assert, not at compile time.
* `cbrt` supports the full `u256` range for `D <= 18`. For larger `D` it
  panics above about `2^(384 - 6.64 D)` raw (`2^204` for Ray).
* The transcendental functions cost more gas than core's rational
  approximations: they sum 20–40 series terms. Use core's `SWad::exp` /
  `Wad::ln` when 18 decimals and roughly 1 ulp are enough. Use Fathom for Ray
  precision, other scales, or `pow`, where its error is about 100 times
  smaller than `powf`'s. Gas has not been benchmarked yet.
* `Accumulator::index_at` rounds the index up. That is correct for debt
  indices; a supply index should probably round down. A per-mode accumulator
  is future work.
* "Property-style" tests use a deterministic in-Fe generator, because Fe has
  no fuzzer. The large-sample evidence comes from the external harness. The
  error bounds are empirical, not formally verified.
* Nothing here has been audited.

## Credits and reuse

* **`core::fixed` and `core::num`** (Fe standard library, argotorg/fe,
  Apache-2.0) provide the types Fathom extends and the 512-bit
  `checked_mul_div` / `mulmod` that every Fathom operation calls. Fathom
  calls this code and copies none of it.
* **Solady** (Vectorized, MIT) `expWad` / `lnWad` / `powWad` / `rpow`, as
  ported into `core::fixed` by the Fe authors, using Remco Bloemen's rational
  approximations. Fathom measures these ports and does not reuse their code.
  `fathom::compound` calls core's `**` (the rpow port).
* **Remco Bloemen's mul-div** (as in Uniswap's FullMath) is used through
  `core::num`.
* Fathom's own `exp`, `ln`, `cbrt`, rounding and rate/AMM code is new. Its
  methods (Taylor series, atanh series, Newton iteration, scaled balances,
  constant-product formulas) are standard textbook and DeFi techniques.

See [docs/SUBMISSION.md](docs/SUBMISSION.md) for the declaration of
pre-existing work and AI assistance.

## License

MIT. See [LICENSE](LICENSE).
