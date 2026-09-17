def convert_phone(number: str) -> str:
    number = number.strip()
    if number.startswith("0"):
        return "+84" + number[1:]
    if number.startswith("+84"):
        return number
    if number.startswith("84"):
        return "+" + number
    return number
