"""Set or clear the DB-backed provider override and/or a provider's
API-key-slot index or model override -- in one combined write+verification
pass when several are given.

    uv run python -m scripts.set_override groq
    uv run python -m scripts.set_override --clear
    uv run python -m scripts.set_override groq --index 1
    uv run python -m scripts.set_override groq --index 1 --no-activate
    uv run python -m scripts.set_override groq --clear-index
    uv run python -m scripts.set_override groq --clear-index --no-activate
    uv run python -m scripts.set_override vertex --model gemini-2.5-flash
    uv run python -m scripts.set_override vertex --model gemini-2.5-flash --no-activate
    uv run python -m scripts.set_override vertex --clear-model --no-activate

The model override is per-provider, not global: setting one only changes the
model used when that specific provider is active, so flipping the active
provider carries each one's own correct model along with it. This is what
makes `set_override.py vertex` safe even though gemini and vertex draw from
different model catalogs -- vertex's own override travels with it, rather
than leaving whatever model the previously-active provider had configured.

Full replacement for scripts/set_provider.py and scripts/set_api_key.py --
see docs/superpowers/specs/2026-08-12-override-cli-unification-design.md
section 5 for the complete mapping table. Both older scripts are temporary
and are NOT modified by this script's existence; they are deleted
separately, after the presentation this was built for.

Verifies against the EFFECTIVE index -- whatever will actually be active
for this provider after the write, not always index 0 -- via
scripts._override.verify_render_slot. This fixes a latent gap in
scripts/set_provider.py, which always verified index 0 regardless of any
existing key-index override for that provider.

Verification is intentionally asymmetric around --clear-index: paired with
--no-activate (clearing the index override without touching which provider
is active), it never verifies at all -- matching old set_api_key.py's
--clear, which also never checked a credential before clearing one.
Reverting to the default slot is exactly the operation an operator reaches
for during a key rotation, precisely when a Render/local mismatch is most
likely, so a clear must not be blockable by a verification failure.
--clear-index WITHOUT --no-activate still verifies (against index 0, the
slot about to become active) because that combination puts a provider into
production and checking its target credential first is a genuine, worthwhile
check.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from app.providers import registry
from app.queue import store
from scripts import _override


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="set_override",
        # Without this, argparse treats a truncated flag like --cle as an
        # abbreviation of --clear and runs it -- scripts/deploy.py,
        # scripts/set_provider.py, and scripts/set_api_key.py all carry the
        # same guard after an identical abbreviation match fired a live
        # production sync.
        allow_abbrev=False,
        description=(
            "Set or clear the DB-backed provider override and/or a provider's "
            "API-key-slot index or model override."
        ),
    )
    parser.add_argument(
        "provider",
        nargs="?",
        help=f"one of: {', '.join(sorted(registry.PROVIDERS))}",
    )
    parser.add_argument(
        "--index", type=int, help="set this provider's key-index override to N"
    )
    parser.add_argument(
        "--clear-index", action="store_true", help="clear this provider's key-index override"
    )
    parser.add_argument(
        "--model", help="set this provider's model override to NAME"
    )
    parser.add_argument(
        "--clear-model", action="store_true", help="clear this provider's model override"
    )
    parser.add_argument(
        "--no-activate",
        action="store_true",
        help="only touch the key-index override; leave the active-provider override untouched",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="clear the provider override (must be used alone)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="write despite a failed live-verification refusal",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)

    if args.clear:
        if (
            args.provider
            or args.index is not None
            or args.clear_index
            or args.no_activate
            or args.model is not None
            or args.clear_model
        ):
            print("--clear must be used alone", file=sys.stderr)
            return 2
        store.init_pool()
        store.set_provider_override(None, datetime.now(timezone.utc).isoformat())
        print("provider override cleared; falling back to LLM_PROVIDER")
        return 0

    if not args.provider:
        print("a provider is required (or --clear)", file=sys.stderr)
        return 2
    if args.provider not in registry.PROVIDERS:
        accepted = ", ".join(sorted(registry.PROVIDERS))
        print(
            f"unsupported provider {args.provider!r} (expected one of: {accepted})",
            file=sys.stderr,
        )
        return 2
    if args.index is not None and args.clear_index:
        print("--index and --clear-index are mutually exclusive", file=sys.stderr)
        return 2
    if args.model is not None and args.clear_model:
        print("--model and --clear-model are mutually exclusive", file=sys.stderr)
        return 2
    if args.model is not None and not args.model.strip():
        print("--model must not be empty", file=sys.stderr)
        return 2
    if (
        args.no_activate
        and args.index is None
        and not args.clear_index
        and args.model is None
        and not args.clear_model
    ):
        print(
            "--no-activate requires --index, --clear-index, --model, or --clear-model",
            file=sys.stderr,
        )
        return 2
    if args.index is not None and args.index < 0:
        print(f"index must be >= 0, got {args.index}", file=sys.stderr)
        return 2

    store.init_pool()

    # A pure "clear the key-index override, leave the active provider
    # alone" never verifies -- same as old set_api_key.py's --clear, which
    # never checked a credential before clearing one either. Clearing is
    # exactly what an operator reaches for during a key rotation, precisely
    # when a Render/local mismatch is most likely, so it must not be
    # refusable. --clear-index WITHOUT --no-activate is NOT covered by this
    # skip -- it also activates the provider at index 0, so verifying that
    # index actually has a credential first is worthwhile and still runs
    # below.
    # Credential verification exists to catch "activating a provider whose key
    # slot has no credential". A call that only touches the model or only
    # clears the index -- and does not activate anything -- puts no credential
    # into production, so there is nothing to verify and a verification failure
    # must not be able to block it.
    _credential_untouched = args.no_activate and args.index is None
    if not _credential_untouched:
        if args.index is not None:
            effective_index = args.index
        elif args.clear_index:
            effective_index = 0
        else:
            effective_index = store.get_key_index_override(args.provider) or 0

        ok, message = _override.verify_render_slot(args.provider, effective_index)
        if ok:
            print(message)
        elif args.force:
            print(f"{message} -- proceeding anyway (--force)", file=sys.stderr)
        else:
            print(f"refusing to set the override: {message}", file=sys.stderr)
            return 2

    # Index write before provider-activation write (not the reverse): these
    # are two separate statements, not one transaction (see the design doc's
    # non-atomic-by-choice decision), so this order picks the safer partial-
    # failure mode -- if the second write never happens, the index changed
    # but the provider isn't active yet, so nothing behavior-visible changes
    # until both succeed. The reverse order would leave a provider active
    # against a stale index if only the first write landed.
    now = datetime.now(timezone.utc).isoformat()
    if args.index is not None:
        store.set_key_index_override(args.provider, args.index, now)
        print(f"{args.provider} key-index override set to {args.index}")
    elif args.clear_index:
        store.set_key_index_override(args.provider, None, now)
        print(f"{args.provider} key-index override cleared")
    # Model write sits between the index write and provider activation, for the
    # same partial-failure reasoning as the index write above: if a later write
    # never happens, the model changed but the provider is not active yet, so
    # nothing behavior-visible has changed. Activating first would leave a
    # provider live against a stale model -- exactly the gemini/vertex breakage
    # this override exists to prevent.
    if args.model is not None:
        store.set_model_override(args.provider, args.model.strip(), now)
        print(f"{args.provider} model override set to {args.model.strip()}")
    elif args.clear_model:
        store.set_model_override(args.provider, None, now)
        print(f"{args.provider} model override cleared")
    if not args.no_activate:
        store.set_provider_override(args.provider, now)
        print(f"provider override set to {args.provider}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
