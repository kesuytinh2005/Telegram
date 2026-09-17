import importlib
import pkgutil
import commands


COMMANDS = {}


def load_commands():
    COMMANDS.clear()

    for module_info in pkgutil.iter_modules(commands.__path__):
        module_name = module_info.name

        if module_name.startswith("_"):
            continue

        try:
            module = importlib.import_module(f"commands.{module_name}")

            info = getattr(module, "COMMAND_INFO", None)

            if not info:
                continue

            command = info.get("command")

            if not command:
                continue

            COMMANDS[command] = {
                **info,
                "module": module,
            }

        except Exception as e:
            print(f"[COMMAND ERROR] {module_name}: {e}")

    return COMMANDS


def get_commands():
    return COMMANDS


def get_command(command):
    return COMMANDS.get(command)