"""The session's command-line grammar as plain data, independent of argparse's help layout."""

import sys

from oscmix_desk.arguments import build_arg_parser


def grammar():
    parser = build_arg_parser()
    options = []
    for action in parser._actions:
        if type(action).__name__ == "_HelpAction":
            continue  # argparse's own, worded by the Python version
        kind = action.type
        options.append({
            "options": list(action.option_strings), "dest": action.dest,
            "action": type(action).__name__,
            "metavar": (list(action.metavar) if isinstance(action.metavar, tuple)
                        else action.metavar),
            "type": getattr(kind, "__name__", None), "default": repr(action.default),
            "nargs": action.nargs, "choices": action.choices, "help": action.help,
        })
    return {"prog": parser.prog, "description": parser.description, "options": options}


if __name__ == "__main__":
    import json

    json.dump(grammar(), sys.stdout, indent=1)
    sys.stdout.write("\n")
