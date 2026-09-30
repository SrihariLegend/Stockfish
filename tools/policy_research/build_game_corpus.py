#!/usr/bin/env python3
"""Freeze a reproducible paired middlegame/endgame corpus from a PGN.

Selection is by whole game. Each selected game contributes one middlegame and
one endgame position; downstream validation must hold out both roots together.
The generated JSON is self-contained and records the source SHA-256, game index,
headers, ply and independent selection/ply seeds.

Requires python-chess; it is a corpus-construction tool, not an engine runtime
dependency.
"""
import argparse
import hashlib
import json
import os
import random

import chess
import chess.pgn


def candidates(game):
    board = game.board()
    middle, end = [], []
    for ply, move in enumerate(game.mainline_moves(), 1):
        board.push(move)
        pieces = len(board.piece_map())
        queens = len(board.pieces(chess.QUEEN, chess.WHITE)) + \
            len(board.pieces(chess.QUEEN, chess.BLACK))
        if 32 <= ply <= 70 and queens == 2 and pieces >= 20 and not board.is_check():
            middle.append((ply, board.fen()))
        if (ply >= 70 and queens == 0 and pieces <= 18 and not board.is_check()
                and not board.is_game_over()):
            end.append((ply, board.fen()))
    return middle, end


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pgn", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--games", type=int, default=12)
    parser.add_argument("--selection-seed", type=int, default=20260906)
    parser.add_argument("--ply-seed", type=int, default=20260907)
    parser.add_argument("--exclude-corpus", action="append", default=[],
                        help="frozen corpus whose source game indexes may not repeat")
    args = parser.parse_args()

    source_sha = hashlib.sha256(open(args.pgn, "rb").read()).hexdigest()
    eligible = []
    with open(args.pgn, errors="replace") as fh:
        index = 0
        while True:
            game = chess.pgn.read_game(fh)
            if game is None:
                break
            index += 1
            middle, end = candidates(game)
            if middle and end:
                eligible.append((index, game, middle, end))
    excluded = set()
    for path in args.exclude_corpus:
        previous = json.load(open(path))
        excluded.update(root["source_game_index"] for root in previous["roots"])
    eligible = [item for item in eligible if item[0] not in excluded]
    rng = random.Random(args.selection_seed)
    chosen = sorted(rng.sample(eligible, args.games), key=lambda x: x[0])
    roots = []
    for index, game, middle, end in chosen:
        game_rng = random.Random((args.ply_seed << 32) ^ index)
        for phase, pool in (("middlegame", middle), ("endgame", end)):
            ply, fen = pool[game_rng.randrange(len(pool))]
            roots.append({
                "id": f"g{index:04d}-{phase[:3]}",
                "game_group": f"game-{index:04d}",
                "phase": phase,
                "fen": fen,
                "source_game_index": index,
                "source_ply": ply,
                "headers": {k: game.headers.get(k, "")
                            for k in ("Event", "Site", "Date", "Round", "White",
                                      "Black", "Result")},
                "engine_seed": (args.selection_seed * 1000003 + index * 2
                                + (phase == "endgame")) & 0x7fffffff,
            })
    out = {
        "schema": "policy-research-game-corpus/1",
        "description": "Seeded paired middlegame/endgame roots; split by game_group.",
        "source": {"basename": os.path.basename(args.pgn), "sha256": source_sha,
                   "eligible_games_after_exclusions": len(eligible),
                   "excluded_game_indexes": sorted(excluded)},
        "selection_seed": args.selection_seed,
        "ply_seed": args.ply_seed,
        "selection_rules": {
            "middlegame": "ply 32..70, both queens, >=20 pieces, not in check",
            "endgame": "ply >=70, no queens, <=18 pieces, not in check, nonterminal",
        },
        "roots": roots,
    }
    with open(args.output, "w") as fh:
        json.dump(out, fh, indent=2)
        fh.write("\n")
    print(f"wrote {len(roots)} roots from {len(chosen)} games to {args.output}")
    print(f"source sha256 {source_sha}")


if __name__ == "__main__":
    main()
