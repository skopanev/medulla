Using your bash tool, run the command `sleep 5` forty times IN A ROW —
one separate tool call each, waiting for every call to finish before the
next (never in the background). After all forty, emit the signal named
done.


## Signal protocol (engine-provided)

To emit a signal, print this template on its own line in your final message,
substituting {name} with the signal's name and the body with a short message
(no backticks, no quotes, keep the angle brackets exactly as shown):

<signal:{name}>short message</signal:{name}>

For example, a signal named finished would be printed as one line starting
with "<signal:" then "finished>", the message, and the matching closing tag.
Emit a signal only when the task tells you to. Print it as plain text in your
answer — never via a shell command or a file.
