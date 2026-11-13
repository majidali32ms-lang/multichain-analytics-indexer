"""Where each planned chain stands on post-quantum signatures, with a source for every row.

This is the one place the status lives. Each row says when it was last checked against its
source; check it again before changing a row or publishing the docs page.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PqState(StrEnum):
    LIVE = "live"
    ENABLED_NO_USE_SEEN = "enabled, no use seen"
    NONE_LIVE = "none live yet"


@dataclass(frozen=True)
class ChainPqStatus:
    chain: str
    network: str
    state: PqState
    scheme: str | None
    since: str
    detected: bool
    note: str
    source: str
    checked_on: str


CHECKED_ON = "2026-11-08"
EVM_CHECKED_ON = "2026-11-09"
BITCOIN_CHECKED_ON = "2026-11-13"

PQ_STATUS: tuple[ChainPqStatus, ...] = (
    ChainPqStatus(
        chain="algorand",
        network="mainnet",
        state=PqState.LIVE,
        scheme="falcon-1024",
        since="in use by 2026-08-22",
        detected=True,
        note=(
            "Native Falcon-1024 accounts: signature type pqsig, scheme f1 (consensus v42). "
            "LogicSigs using falcon_verify are possible but not identified. State proof "
            "transactions never count."
        ),
        source="https://github.com/algorand/go-algorand/blob/master/config/consensus.go",
        checked_on=CHECKED_ON,
    ),
    ChainPqStatus(
        chain="algorand",
        network="testnet",
        state=PqState.LIVE,
        scheme="falcon-1024",
        since="in use by 2026-09-07",
        detected=True,
        note="As on mainnet.",
        source="https://github.com/algorand/go-algorand/blob/master/config/consensus.go",
        checked_on=CHECKED_ON,
    ),
    ChainPqStatus(
        chain="solana",
        network="mainnet",
        state=PqState.NONE_LIVE,
        scheme=None,
        since="",
        detected=False,
        note=(
            "The Winternitz vault program in Blueshift's repository "
            "(wntrRTssxbf1rz9RPJ4xNBbpXxfsidQJT177NN3MXhB) is not deployed on mainnet, "
            "devnet or testnet."
        ),
        source="https://github.com/blueshift-gg/solana-winternitz-vault",
        checked_on=CHECKED_ON,
    ),
    ChainPqStatus(
        chain="near",
        network="mainnet",
        state=PqState.ENABLED_NO_USE_SEEN,
        scheme="ml-dsa-65",
        since="enabled after 2026-07-20",
        detected=True,
        note=f"ML-DSA-65 keys and signatures (nearcore 2.13.0). No use seen as of {CHECKED_ON}.",
        source="https://github.com/near/nearcore/releases/tag/2.13.0",
        checked_on=CHECKED_ON,
    ),
    ChainPqStatus(
        chain="near",
        network="testnet",
        state=PqState.LIVE,
        scheme="ml-dsa-65",
        since="2026-07-01 (protocol 85)",
        detected=True,
        note="ML-DSA-65 keys and signatures; NEP-645 is still a draft.",
        source="https://github.com/near/NEPs/blob/master/neps/nep-0645.md",
        checked_on=CHECKED_ON,
    ),
    ChainPqStatus(
        chain="ethereum",
        network="mainnet",
        state=PqState.NONE_LIVE,
        scheme=None,
        since="",
        detected=False,
        note=(
            "EIP-8141 (frame transactions) is a draft, scheduled for the Hegota upgrade, "
            "which has no activation date."
        ),
        source="https://ethereum.org/roadmap/security/quantum-resistance/",
        checked_on=EVM_CHECKED_ON,
    ),
    ChainPqStatus(
        chain="arbitrum",
        network="mainnet",
        state=PqState.NONE_LIVE,
        scheme=None,
        since="",
        detected=False,
        note="No official post-quantum statement found in the Arbitrum docs or blog.",
        source="https://docs.arbitrum.io/",
        checked_on=EVM_CHECKED_ON,
    ),
    ChainPqStatus(
        chain="optimism",
        network="mainnet",
        state=PqState.NONE_LIVE,
        scheme=None,
        since="",
        detected=False,
        note=(
            "A roadmap exists, nothing is live: the Post-Quantum Roadmap for the Superchain "
            "(2026-01-14) plans to retire ECDSA accounts by January 2036, subject to "
            "governance, and has not chosen a scheme."
        ),
        source="https://optimism.io/blog/a-post-quantum-roadmap-for-the-superchain",
        checked_on=EVM_CHECKED_ON,
    ),
    ChainPqStatus(
        chain="base",
        network="mainnet",
        state=PqState.NONE_LIVE,
        scheme=None,
        since="",
        detected=False,
        note=(
            "No official post-quantum statement found. Optimism's Superchain roadmap does not "
            "name Base, which has run its own stack since its Azul upgrade (2026); its "
            "P256VERIFY precompile is not post-quantum."
        ),
        source="https://base.org/blog/introducing-base-azul",
        checked_on=EVM_CHECKED_ON,
    ),
    ChainPqStatus(
        chain="polygon",
        network="mainnet",
        state=PqState.NONE_LIVE,
        scheme=None,
        since="",
        detected=False,
        note=(
            "No official post-quantum statement found in the Polygon PoS docs, blog or "
            "improvement proposals."
        ),
        source="https://docs.polygon.technology/pos/overview/",
        checked_on=EVM_CHECKED_ON,
    ),
    ChainPqStatus(
        chain="bnb",
        network="mainnet",
        state=PqState.NONE_LIVE,
        scheme=None,
        since="",
        detected=False,
        note=(
            "Research only: a migration report (2026-05-14) benchmarked ML-DSA-44 transaction "
            "signatures; its proof of concept was not merged, and no BEP enables them."
        ),
        source="https://www.bnbchain.org/en/blog/bsc-post-quantum-cryptography-migration-report",
        checked_on=EVM_CHECKED_ON,
    ),
    ChainPqStatus(
        chain="bitcoin",
        network="mainnet",
        state=PqState.NONE_LIVE,
        scheme=None,
        since="",
        detected=False,
        note=(
            "BIP-360 (P2MR, a witness v2 output with no key path) and BIP-361 (migration "
            "and legacy signature sunset) are drafts with no activation parameters; no "
            "post-quantum signature BIP exists, and Bitcoin Core 32.1 adds none."
        ),
        source="https://github.com/bitcoin/bips/blob/master/bip-0360.mediawiki",
        checked_on=BITCOIN_CHECKED_ON,
    ),
)
