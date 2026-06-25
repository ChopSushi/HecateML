import asyncio

from google.adk.runners import InMemoryRunner
from google.genai import types

from my_agent.agent import root_agent


async def ask(runner, session_id, question):
    print(f"\n=== Q: {question} ===")
    msg = types.Content(role="user", parts=[types.Part(text=question)])
    async for event in runner.run_async(user_id="u1", session_id=session_id, new_message=msg):
        for part in (event.content.parts if event.content else []):
            if getattr(part, "function_call", None):
                print("[tool call]", part.function_call.name, dict(part.function_call.args))
            if getattr(part, "function_response", None):
                resp = part.function_response.response
                print("[tool result]", str(resp)[:160], "...")
            if getattr(part, "text", None):
                print("[answer]", part.text)


async def main():
    runner = InMemoryRunner(agent=root_agent, app_name="smoke")
    session = await runner.session_service.create_session(app_name="smoke", user_id="u1")
    await ask(runner, session.id, "What's the regulatory history of Basel III? Keep it brief.")


asyncio.run(main())
