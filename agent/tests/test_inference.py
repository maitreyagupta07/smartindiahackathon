import asyncio

from clients.inference_client import call_inference


async def main():
    print("Calling Person A's Qwen model...")

    result = await call_inference(
        model="qwen2.5:1.5b-instruct",
        prompt="give a life biopic on robert frost in 10 sentences",
    )

    print("\nModel response:")
    print(result)


if __name__ == "__main__":
    asyncio.run(main())