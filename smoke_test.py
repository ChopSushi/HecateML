import asyncio
from google.adk.runners import InMemoryRunner
from google.genai import types
from my_agent.agent import root_agent


async def main():
    runner = InMemoryRunner(agent=root_agent, app_name="smoke")
    session = await runner.session_service.create_session(app_name="smoke", user_id="u1")
    msg = types.Content(role="user",
                        parts=[types.Part(text="How many letters are in the word 'interview'?")])
    async for event in runner.run_async(user_id="u1", session_id=session.id, new_message=msg):
        for part in (event.content.parts if event.content else []):
            if getattr(part, "function_call", None):
                print("[tool call]", part.function_call.name, dict(part.function_call.args))
            if getattr(part, "function_response", None):
                print("[tool result]", part.function_response.response)
            if getattr(part, "text", None):
                print("[final]", part.text)


asyncio.run(main())
