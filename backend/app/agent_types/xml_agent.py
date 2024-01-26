from langchain.schema.messages import FunctionMessage
from langchain_core.language_models.base import LanguageModelLike

from langchain_core.messages import SystemMessage, AIMessage, HumanMessage

from langchain.tools import BaseTool
from langgraph.checkpoint import BaseCheckpointSaver
from langgraph.graph.message import MessageGraph
from langgraph.graph import END
from langgraph.prebuilt import ToolExecutor
from langgraph.prebuilt import ToolInvocation

from langchain.tools.render import render_text_description

from app.agent_types.prompts import xml_template


def _collapse_messages(messages):
    log = ""
    if isinstance(messages[-1], AIMessage):
        scratchpad = messages[:-1]
        final = messages[-1]
    else:
        scratchpad = messages
        final = None
    if len(scratchpad) % 2 != 0:
        raise ValueError("Unexpected")
    for i in range(0, len(scratchpad), 2):
        action = messages[i]
        observation = messages[i + 1]
        log += f"{action.content}<observation>{observation.content}</observation>"
    if final is not None:
        log += final.content
    return AIMessage(content=log)


def construct_chat_history(messages):
    collapsed_messages = []
    temp_messages = []
    for message in messages:
        if isinstance(message, HumanMessage):
            if temp_messages:
                collapsed_messages.append(_collapse_messages(temp_messages))
                temp_messages = []
            collapsed_messages.append(message)
        else:
            temp_messages.append(message)

    # Don't forget to add the last non-human message if it exists
    if temp_messages:
        collapsed_messages.append(_collapse_messages(temp_messages))

    return collapsed_messages


def get_xml_agent_executor(
    tools: list[BaseTool],
    llm: LanguageModelLike,
    system_message: str,
    checkpoint: BaseCheckpointSaver,
):
    formatted_system_message = xml_template.format(
        system_message=system_message,
        tools=render_text_description(tools),
        tool_names=", ".join([t.name for t in tools]),
    )

    llm_with_stop = llm.bind(stop=["</tool_input>"])

    def _get_messages(messages):
        return [
            SystemMessage(content=formatted_system_message)
        ] + construct_chat_history(messages)

    agent = _get_messages | llm_with_stop
    tool_executor = ToolExecutor(tools)

    # Define the function that determines whether to continue or not
    def should_continue(messages):
        last_message = messages[-1]
        if "</tool>" in last_message.content:
            return "continue"
        else:
            return "end"

    # Define the function to execute tools
    async def call_tool(messages):
        # Based on the continue condition
        # we know the last message involves a function call
        last_message = messages[-1]
        # We construct an ToolInvocation from the function_call
        tool, tool_input = last_message.content.split("</tool>")
        _tool = tool.split("<tool>")[1]
        _tool_input = tool_input.split("<tool_input>")[1]
        if "</tool_input>" in _tool_input:
            _tool_input = _tool_input.split("</tool_input>")[0]
        action = ToolInvocation(
            tool=_tool,
            tool_input=_tool_input,
        )
        # We call the tool_executor and get back a response
        response = await tool_executor.ainvoke(action)
        # We use the response to create a FunctionMessage
        function_message = FunctionMessage(content=str(response), name=action.tool)
        # We return a list, because this will get added to the existing list
        return function_message

    workflow = MessageGraph()

    # Define the two nodes we will cycle between
    workflow.add_node("agent", agent)
    workflow.add_node("action", call_tool)

    # Set the entrypoint as `agent`
    # This means that this node is the first one called
    workflow.set_entry_point("agent")

    # We now add a conditional edge
    workflow.add_conditional_edges(
        # First, we define the start node. We use `agent`.
        # This means these are the edges taken after the `agent` node is called.
        "agent",
        # Next, we pass in the function that will determine which node is called next.
        should_continue,
        # Finally we pass in a mapping.
        # The keys are strings, and the values are other nodes.
        # END is a special node marking that the graph should finish.
        # What will happen is we will call `should_continue`, and then the output of that
        # will be matched against the keys in this mapping.
        # Based on which one it matches, that node will then be called.
        {
            # If `tools`, then we call the tool node.
            "continue": "action",
            # Otherwise we finish.
            "end": END,
        },
    )

    # We now add a normal edge from `tools` to `agent`.
    # This means that after `tools` is called, `agent` node is called next.
    workflow.add_edge("action", "agent")

    # Finally, we compile it!
    # This compiles it into a LangChain Runnable,
    # meaning you can use it as you would any other runnable
    app = workflow.compile(checkpointer=checkpoint)
    return app
