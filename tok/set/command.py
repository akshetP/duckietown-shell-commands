import webbrowser

import termcolor
from dt_authentication import InvalidToken, DuckietownToken

from dt_shell import DTCommandAbs

token_dt1_config_key = "token_dt1"

from dt_shell import DTShell


class DTCommand(DTCommandAbs):
    @staticmethod
    def command(shell: DTShell, args):
        link = "https://www.duckietown.org/site/your-token"
        example = "dt1-7vEuJsaxeXXXXX-43dzqWFnWd8KBa1yev1g3UKnzVxZkkTbfSJnxzuJjWaANeMf4y6XSXBWTpJ7vWXXXX"
        msg = """
Please enter your Duckietown token.

It looks something like this:

    {example}

To find your token, first login to duckietown.org, and open the page:

    {link}


Enter token: """.format(
            link=href(link), example=dark(example)
        )

        shell.sprint("args: %s" % args.__repr__())

        if args:
            val_in = args[0]
        else:
            webbrowser.open(link, new=2)
            val_in = input(msg)

        s = val_in.strip()
        try:
            token = DuckietownToken.from_string(s)
            if token.uid == -1:
                msg = "This is the sample token. Please use your own token."
                raise ValueError(msg)
            shell.sprint("Correctly identified as uid = %s" % token.uid)
        except InvalidToken as e:
            msg = 'The string "%s" does not look like a valid token:\n%s' % (s, e)
            shell.sprint(msg)
            return

        shell.shell_config.token_dt1 = s
        shell.save_config()


def dark(x):
    return termcolor.colored(x, attrs=["dark"])


def href(x):
    return termcolor.colored(x, "blue", attrs=["underline"])
