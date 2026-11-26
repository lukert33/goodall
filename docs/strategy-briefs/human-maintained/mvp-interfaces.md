# MVP Interfaces
Prior decisions in sketches of this project have implied that I would certainly use only Claude Code as the LLM provider to build goodall against.

Upon further consideration, I am challenging that assumption. For one thing, I am detecting the possibility of bias where Claude steers me to build for Claude first, leaving others later.

For another, I want to really excercise the adaptors in Entire CLI to see how much I can get for the cheap cost of interfacing with that vendor library at the right levels.

Finally, with reference to the goal of goodall providing a user with insights with the report draft compliled and displayed at the close of a session. I think that a demo which shows that goodall can capture how multiple agents from multiple providers fed in to one decision or a tightly coupled set of decisions is quite a bit more compelling than a visualization of how a single turn based interaction with a single agent netted out.


### Speculative Scope Extenstions
1. I also think it might be worth elevating the choice of task tracking system to an early phase. I am not ready to commit, but because the status of tracked tasks is so important to a well run project, it also seems like it would be a high priority data source for goodall worth thinking about in early phases.