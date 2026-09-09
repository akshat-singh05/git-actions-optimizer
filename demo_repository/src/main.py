"""Main application entry point."""
from src.service import process_request
from src.utils import format_output


def main():
    """Run the main application."""
    result = process_request("demo_input")
    return format_output(result)


if __name__ == "__main__":
    print(main())
