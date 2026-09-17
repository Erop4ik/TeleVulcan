"""Proof-of-work «капча» страницы логина dziennik-logowanie.vulcan.net.pl.

Порт captcha.js: для каждого раунда ищется nonce, при котором первые 4 байта
SHA-256(challenge + предыдущие_ответы + nonce) (big-endian, unsigned) < difficulty.
Ответ — nonce'ы через ';'.
"""
import hashlib


def _solve_round(challenge: str, difficulty: int) -> int:
    base = challenge.encode("latin-1", "replace")
    nonce = 1
    while nonce < 1_000_000_000:
        digest = hashlib.sha256(base + str(nonce).encode()).digest()
        if int.from_bytes(digest[:4], "big") < difficulty:
            return nonce
        nonce += 1
    raise RuntimeError("PoW: решение не найдено")


def solve(challenge: str, difficulty: int, rounds: int) -> str:
    results: list[int] = []
    for _ in range(rounds):
        results.append(_solve_round(challenge + "".join(map(str, results)), difficulty))
    return ";".join(map(str, results))
