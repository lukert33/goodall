# WildChat: personal information in free text after automated redaction. A case study

*Researched 2026-11-26 from live sources by an AI research agent (Claude Opus), at the project owner's request. Every claim links to its source. Section 7 separates what the agent verified from what it could not confirm. Nobody has yet checked the agent's reading of these sources independently, so treat the figures as leads to confirm before citing them.*

## Summary (five lines)

1. WildChat (AI2 and collaborators, 2023–2025) is about one million conversations, later 4.8 million, collected through a free ChatGPT front end. Users gave opt-in consent, confirmed twice, to having their chats published.
2. Before release the authors ran Microsoft Presidio with spaCy and hand-written rules. These removed names, phone numbers, emails, credit cards and URLs. IP addresses were hashed, and only the country and state were kept.
3. A COLM-2024 study ("Trust No Bot") ran a stronger PII detector on the already-scrubbed data. It found detected PII in over 70% of queries, mostly names and organizations. It also found sensitive topics, such as sex, health and drug use, that no PII detector targets.
4. The actual "trouble" was modest and handled in public. Researchers notified the authors, who quietly removed flagged conversations in October 2024. A text report of child sexual abuse material (CSAM) in July 2024 led to a split into a public "clean" version and a gated "Full" version. I found no regulator action, press scandal, takedown or documented harm.
5. The lasting lesson is that scrubbers catch formatted identifiers, but not context, third parties, or linkability across a contributor's sessions. In WildChat, the problems were found by people reading conversations, not by the tool.

---

## 1. Timeline

| Date | Event | Source |
|---|---|---|
| 2023-04-09 | Collection begins: two chatbot services on Hugging Face Spaces (GPT-3.5-Turbo and GPT-4 APIs), no account needed | [WildChat paper §2](https://arxiv.org/html/2405.01470v1) |
| 2023 (ICLR submission) | Earlier version titled "(InThe)WildChat: 570K ChatGPT Interaction Logs In The Wild"; potentially unsafe portion "on request" | [ICLR 2024 poster](https://iclr.cc/virtual/2024/poster/19206) (search snippet; page not read in full) |
| 2023-11-10 | `allenai/WildChat` (about 650K) published on Hugging Face under the **AI2 ImpACT Low-Risk license**, a click-through license with use restrictions | [commit history](https://huggingface.co/datasets/allenai/WildChat/commits/main), [license at commit c9c9f4d](https://huggingface.co/datasets/allenai/WildChat/raw/c9c9f4d/LICENSE.md) |
| 2024-05-01 | End of the collection window reported in the 1M paper | [paper §2](https://arxiv.org/html/2405.01470v1) |
| 2024-05-02/03 | 1M paper on arXiv; `allenai/WildChat-1M` published | [arXiv 2405.01470](https://arxiv.org/abs/2405.01470), [1M commits](https://huggingface.co/datasets/allenai/WildChat-1M/commits/main) |
| 2024-06-19 | Journalism study (Brigham, Gao, Kohno, Roesner, Mireshghallah) posted. It identified news agencies in WildChat by matching chats to published articles | [arXiv 2406.13706](https://arxiv.org/abs/2406.13706) |
| 2024-06-26 | License changed from ImpACT to **ODC-BY**, a permissive attribution-only license, applied retroactively to earlier downloads | [WildChat-1M card](https://huggingface.co/datasets/allenai/WildChat-1M/raw/main/README.md) |
| 2024-07-16 | "Trust No Bot" posted (v2 on 07-20) | [arXiv 2407.11438](https://arxiv.org/abs/2407.11438) |
| 2024-07-19 | Hugging Face staff relay a tip that the dataset contains CSAM text | [HF discussion #4](https://huggingface.co/datasets/allenai/WildChat/discussions/4) |
| 2024-07-22/24 | All toxic conversations (flagged by OpenAI Moderation or Detoxify) removed from the public sets; gated `WildChat-1M-Full` created (manual approval plus a justification, "sexual/minors" excluded) | [1M card](https://huggingface.co/datasets/allenai/WildChat-1M/raw/main/README.md), [HF discussion #4](https://huggingface.co/datasets/allenai/WildChat/discussions/4) |
| 2024-09 | WildVis, the authors' search and visualization tool, published. It can filter by hashed IP | [arXiv 2409.03753](https://arxiv.org/html/2409.03753) |
| 2024-10-17 | Conversations flagged "for PII or sensitive information" by Mireshghallah and collaborators removed. The card cites the journalism study | [WildChat card](https://huggingface.co/datasets/allenai/WildChat/raw/main/README.md), [1M commits](https://huggingface.co/datasets/allenai/WildChat-1M/commits/main) |
| 2024-12-03 | Journalism study v2, retitled "Developing Story"; says WildChat removed the identified conversations | [arXiv 2406.13706v2](https://arxiv.org/html/2406.13706) |
| 2025-08-11 | **WildChat-4.8M** (data before 2025-08-01): adds TruffleHog secret scanning and "manual adjustments"; minors-related conversations removed; data-removal request process published | [4.8M card](https://huggingface.co/datasets/allenai/WildChat-4.8M/raw/main/README.md) |
| 2026-09-27 | Preprint shows a provider-style attacker can link held-out WildChat conversations to known users from writing alone | [arXiv 2609.33903](https://arxiv.org/abs/2609.33903) |

## 2. Collection, consent, and what was redacted

**Consent.** Users saw a terms screen before reaching the chat ([paper Appendix B](https://arxiv.org/html/2405.01470v1)). It said the service may record their inputs, the model's outputs, and device and connection details. It said the data may be used for research and product development, may be published or shared with third parties, and may be kept as long as needed. A second pop-up asked users to confirm specifically that their data may be published, and only "Yes" led to the chat. The appendix mentions no age requirement, no withdrawal or deletion right, and no review of one's own conversations before release. The paper notes the data releases went through AI2 legal review, and mentions no IRB (ethics board) approval. A withdrawal route appeared only with the 4.8M card in 2025: users can email the maintainer with conversation hashes ([4.8M card](https://huggingface.co/datasets/allenai/WildChat-4.8M/raw/main/README.md)).

Note what this consent covers. Users agreed to publication of *their own* text. People they mentioned, such as a journalist's source or an immigration client, agreed to nothing.

**Redaction**, from [paper §2 and §7](https://arxiv.org/html/2405.01470v1) and the [dataset cards](https://huggingface.co/datasets/allenai/WildChat-1M/raw/main/README.md):

- **Tools.** Microsoft Presidio, a framework that combines pattern rules with named-entity recognition (NER, a model that tags spans as PERSON, LOCATION and so on), with spaCy as the NER model, plus custom rules. In 4.8M these were joined by TruffleHog, which finds API keys and other secrets, and by "manual adjustments" ([4.8M card](https://huggingface.co/datasets/allenai/WildChat-4.8M/raw/main/README.md)).
- **Entity types.** Names, phone numbers, emails, credit cards and URLs, in ten languages. A `redacted` flag marks conversations and turns where something was replaced.
- **Network metadata.** IP addresses were geolocated (GeoLite2/GeoIP2) to country and state, then hashed. The hash function is not stated. The `header` field (user-agent, accept-language) is released.
- **Linkability, as stated by the authors.** The paper says hashed IP plus headers could let researchers link conversations from the same user. In the same document, §7 says it is not feasible to trace a conversation back to an individual user. Both are paraphrased from the [paper](https://arxiv.org/html/2405.01470v1).
- **Stated limits.** Section 7 concedes users may still have included personal information, and does not claim the removal is complete. No precision or recall figures for the scrubber were published.
- **Content filtering (separate from PII).** In the paper, 10.46% of user turns were flagged toxic. From July 2024, toxic conversations went only into the gated "Full" set. 4.8M dropped 1,543,476 toxic conversations from the public set ([4.8M card](https://huggingface.co/datasets/allenai/WildChat-4.8M/raw/main/README.md)).

## 3. What was found afterward

### 3a. "Trust No Bot" (Mireshghallah, Antoniak, More, Choi, Farnadi; [arXiv 2407.11438](https://arxiv.org/html/2407.11438v2), July 2024)

- **Data.** WildChat after Presidio. The paper says WildChat's curators had already done one round of PII removal with Presidio, and that Presidio often misses PII that is not well formatted (§3.2). The main sample was 5,000 conversations: English only, one per hashed IP, deduplicated, with user turns of at least 20 characters. The PII analysis ran on "selected subsamples," whose exact sizes are not given in the text. A ShareGPT sample served as a comparison.
- **Detector.** Azure AI Language PII, a commercial cloud detector with 20+ categories. The authors spot-checked its output and dropped categories with high error rates, such as passport and national ID. They kept IBAN because its hits were mostly API tokens, which are secrets even if not bank numbers.
- **The headline figure.** "Over 70% of queries contain some kind of detected PII." This is in the Introduction and is not repeated as a number in §3.
  - *Unit:* per query (user turn), not per conversation or per user.
  - *What counts:* any span the detector flagged. The bulk are **person names and organization names**. Emails, street addresses and IP addresses were the rarest categories.
  - *What it does not establish:* whether a name is real, fictional or a public figure. §4.2 says the names in storytelling and erotic contexts may or may not be fictional. So 70% is an upper-bound signal of *detectable identifiers left after scrubbing*. It is not 70% of conversations exposing a real person.
- **Task context.** About 48% of translation queries and 16% of code-editing queries contained detected PII. People paste whole documents (letters, emails, code with tokens) to get them translated or fixed.
- **Sensitive topics a PII detector does not target.** Using GPT-4 labels, spot-checked at about 87% mean accuracy, the categories as a share of the 5k sample were:

  | Topic | Share of the 5k sample |
  |---|---|
  | Academic | 29.9% |
  | Quoted code | 19.5% |
  | Fandom | 14.0% |
  | Hobbies | 8.7% |
  | Financial and corporate | 7.2% |
  | Sexual and erotic | 6.3% |
  | Job, visa and other applications | 4.2% |
  | Healthcare | 4.1% |
  | Personal relationships | 3.3% |
  | Emotions and mental health | 2.0% |
  | Politics and religion | 0.7% |

  The Introduction says almost 15% of queries mention a non-PII sensitive topic. §4.2 says PII detectors flag "at best a minority" of these.
- **Third parties.** The paper notes disclosures about other people. Examples are a recommendation letter naming someone, and stories naming real people known to the user. It also found jailbreak attempts asking the model for phone numbers and addresses.
- **Action.** The authors notified the WildChat maintainers before publication (§8 and Conclusion).

### 3b. The journalism study ([arXiv 2406.13706](https://arxiv.org/html/2406.13706), June/Dec 2024)

- **What they found.** 78 conversations from two news agencies, identified by matching chats to 111 published articles. That matching is itself a re-identification of organizations from content, with no metadata needed.
- **What the chats contained.**
  - About 9% of the material users pasted in was potentially private conversations.
  - One pasted WhatsApp interview disclosed a source's phone number and sensitive details about the source's children.
  - Unpublished emails from sources and editors were also pasted in.
- **Which tool caught it.** None. The researchers did use an NER model to *surface* candidate conversations with several PII types. A person then judged sensitivity in context.
- **Action.** WildChat removed the conversations. This is the removal the 2024-10-17 changelog cites.

### 3c. The authors' own tooling showed linkability ([WildVis, arXiv 2409.03753](https://arxiv.org/html/2409.03753))

WildVis lets anyone click a hashed IP and list all of that user's conversations. In a worked example, the authors searched for a "Visa Officer" persona plus PII and filtered by that IP. The user appeared to be with an immigration firm and to have disclosed client information. The paper presents this as a discovered misuse case and does not discuss re-identification risk.

### 3d. Stylometric linking, 2026 ([arXiv 2609.33903](https://arxiv.org/abs/2609.33903), preprint, not peer reviewed)

- **Setup.** A user is defined as hashed IP plus two headers, following Zhang et al., EMNLP 2025. An attacker holds each user's earlier conversations and tries to attribute new anonymized ones.
- **WildChat result.** The attacker re-identified at least one conversation for up to about 10% of users.
- **SWE-Chat result.** This is a set of developer sessions with coding agents, mainly Claude Code. There it reached about 50–75% of users, even after file paths, URLs, GitHub usernames and repository names were redacted. The authors attribute the gap to more documents per user and fewer candidate users.
- **Defenses.** None of the text defenses they tested substantially reduced the risk. These were an LLM scrubber, style rewriting and their own embedding defense. A differential-privacy rewrite did reduce it, but made the text unintelligible.

## 4. Consequences

**What happened** (all verified on the dataset cards and Hugging Face):

- **Silent removals with brief changelog lines.** Toxic content was removed in July 2024, and PII or sensitive conversations in October 2024. No counts were given for the PII removal.
- **A gated tier.** The "Full" sets require manual approval plus a justification. They were created in response to the CSAM report, not because of PII.
- **A license change.** It went from ImpACT, a click-through license with use restrictions, to ODC-BY, which has no use restrictions and no anti-re-identification clause, retroactively. This made access *looser*, not tighter.
- **Changes in 4.8M.** It added secret scanning, manual adjustments, minors removal and a removal-request channel.

**What I did not find:** regulator action, lawsuits, mainstream press coverage, a Hugging Face takedown of the whole dataset, or any documented harm to a person. I also found no re-identification of a named individual. The closest are the agency matching in 3b and the attribution of a user to an employer type in 3c. My press search was three web searches, so this is an absence of evidence, not proof of absence.

**Verdict.** It was less trouble than "what went wrong" implies. The record shows a scrubber that worked as designed on formatted identifiers. Researchers then showed that the design target was too narrow, and the maintainers responded quickly with removals. The weaknesses that remain are structural:

- The redaction method is still Presidio-style.
- Hashed IPs still allow linking a user's conversations.
- The public, ungated tier is now *more* permissive than at launch.

## 5. Comparable datasets (only where a primary source is concrete)

- **LMSYS-Chat-1M** ([card](https://huggingface.co/datasets/lmsys/lmsys-chat-1m), [paper](https://arxiv.org/abs/2309.11998)):
  - *Consent:* through the site's terms of use.
  - *Redaction:* the authors removed conversations they judged to contain PII. They replaced person names with placeholders (NAME_1) using OpaquePrompts, and warn this occasionally produces wrong redactions.
  - *Access:* gated behind a license agreement that forbids trying to identify individuals or infer sensitive data, forbids redistribution, and lets the licensor demand deletion.
  - *Contrast with WildChat:* access is restricted, and the restriction is enforced by contract.
- **ShareGPT.** In Trust No Bot's comparison, its PII distribution looked similar to WildChat's ([§3](https://arxiv.org/html/2407.11438v2)). I found no primary source describing ShareGPT's own redaction.

## 6. Lessons for a tool that shares real work records with AI agents

Each lesson is marked **[Evidence]** when a source above supports it, or **[Inference]** when it is my reasoning.

1. **Scrubbers reliably catch formatted identifiers and little else. [Evidence]**
   - *Caught:* emails, phone numbers, card numbers and URLs follow fixed patterns. Secrets such as API keys can be checked with a secret scanner like TruffleHog.
   - *Missed:* names outside clean prose, organizations, case-specific identifiers, and anything sensitive that is not an "entity."
   - *Benchmark numbers:* On the Text Anonymization Benchmark of court cases, default Presidio caught about 70% of identifier mentions. It caught only about 46% of *direct identifiers* (things that alone identify a person) counted per entity, because it missed case numbers entirely ([Pilán et al., Computational Linguistics 48(4), 2022, Table 6](https://aclanthology.org/2022.cl-4.19/)). A generic NER model had better recall but masked more than twice what was needed.
   - *In WildChat:* stronger detectors still flagged PII in most queries after the scrubber had run ([Trust No Bot](https://arxiv.org/html/2407.11438v2)).
2. **The privacy unit is the conversation and the person, not the token. [Evidence]** Sensitive topics (health, sexuality, immigration, mental health) and **quasi-identifiers** carry the risk. A quasi-identifier is a detail that identifies someone only in combination: an employer, a project, a rare situation. Both sources show the harmful cases were contextual: journalists' sources, immigration clients. Work transcripts are full of quasi-identifiers such as repository names, colleagues, customers and internal URLs. **[Inference]**
3. **Third parties are the biggest gap in consent. [Evidence that they appear; inference about the remedy.]** Users pasted other people's messages, letters and interviews. A volunteer can consent for themselves only. A tool should treat pasted correspondence and named colleagues as a separate risk class.
4. **Linkability survives redaction. [Evidence, preprint.]**
   - A stable pseudonym (hashed IP, contributor ID) lets anyone assemble a person's whole history, as WildVis demonstrates.
   - Writing and task content alone linked coding-agent users at 50–75% after path and repository redaction ([arXiv 2609.33903](https://arxiv.org/abs/2609.33903)).
   - Implication **[Inference]**: for a small group of volunteers doing distinctive work, assume their records are re-identifiable to anyone who knows them. Design consent around that, rather than promising anonymity.
5. **Human review found what automation missed. [Evidence]** Every substantive WildChat problem was found by people reading conversations, sometimes with a detector used to *rank candidates* for reading. The supported pattern is "detector for triage, human for judgment."
6. **Participant review before release. [Inference: no WildChat source tests it.]** WildChat had no pre-release review by contributors and only a later email-to-remove channel. For a small volunteer pool, letting each contributor see and strike their redacted record before it is published is the most direct fix for points 2 and 3. I found no primary source measuring how well it works.
7. **Narrow capture and prefer structured fields. [Inference]** Most leaks came from free text pasted for translation or editing. Structured fields avoid this: tool names, timings, actor labels, outcome codes. So does capturing metadata by default and free text only on an opt-in basis.
8. **Gated access is a real but limited mitigation. [Evidence that it is used; no evidence on how well it works.]** LMSYS uses a click-through contract that bans re-identification. WildChat uses manual approval for the Full set. Neither source measures whether gating prevented misuse. WildChat also shows that access terms can drift looser over time.
9. **Publish a changelog and a removal path from day one. [Evidence that WildChat added these later; inference that this is good practice.]** WildChat's removals were credible because they were dated in the card. Removal counts and a contact for withdrawal came late or not at all.

## 7. Verified versus unverified

**Verified today against the live source:**

- *Consent:* a terms screen plus a second confirmation pop-up for publication, with no age, withdrawal or deletion terms in Appendix B (paper).
- *Redaction:* Presidio plus spaCy plus custom rules; entity types names, phones, emails, cards and URLs; hashed IP with state and country; headers released (paper, cards).
- *Size:* 1,039,785 conversations and 204,736 unique IPs (paper). Toxic user turns at 10.46% (paper).
- *License and access:* ImpACT LR first; ODC-BY from 2024-06-26, retroactive; Full sets gated; LMSYS gating terms and NAME_1 redaction (license files, cards).
- *Changelog:* removals on 2024-07-22 and 2024-10-17, and 4.8M changes on 2025-08-11 (cards, commit histories).
- *CSAM report:* the 2024-07-19 report and the responses to it (HF discussion #4).
- *Trust No Bot:* method, the 70% claim and its unit, the topic table, the 48% and 16% figures, and notification of the maintainers.
- *Journalism study:* 78 conversations, the 9% figure, the source's phone number, and the removal.
- *WildVis:* the hashed-IP filter and the immigration-firm example.
- *2026 preprint:* the attribution rates for WildChat and SWE-Chat.
- *TAB:* Presidio results.

**Believed but not confirmed:**

- That Trust No Bot appeared at **COLM 2024**. Secondary sources say so, and the arXiv page lists no venue.
- That Trust No Bot's own findings, as opposed to the journalism study, triggered any removal. The card credits "Mireshghallah and collaborators" and cites only the journalism paper.
- The number of conversations removed for PII in October 2024. It is not published.
- How minors were identified for removal in 4.8M. The card does not say. The HF discussion says "sexual/minors" Moderation flags were used for the 1M sets.
- The ICLR-stage "570K" version details. These come from search snippets; I did not read that page.
- The hash function used for IPs, including whether it is salted. It is not stated anywhere I read.
- Any press, regulator or legal attention. None was found in three searches.

**Not fetched:** the full WildChat PDF, which was too large, so I used the arXiv HTML v1 instead. I did not read the Chatbot Arena terms of use.

## 8. Sources

- WildChat paper, arXiv 2405.01470 v1 (2024-05-02): https://arxiv.org/abs/2405.01470 and https://arxiv.org/html/2405.01470v1
- ICLR 2024 poster: https://iclr.cc/virtual/2024/poster/19206
- WildChat-1M card: https://huggingface.co/datasets/allenai/WildChat-1M/raw/main/README.md and commits: https://huggingface.co/datasets/allenai/WildChat-1M/commits/main
- WildChat (650K) card: https://huggingface.co/datasets/allenai/WildChat/raw/main/README.md and commits: https://huggingface.co/datasets/allenai/WildChat/commits/main
- Original ImpACT license (commit c9c9f4d): https://huggingface.co/datasets/allenai/WildChat/raw/c9c9f4d/LICENSE.md and current ODC-BY: https://huggingface.co/datasets/allenai/WildChat/raw/main/LICENSE.md
- WildChat-4.8M card: https://huggingface.co/datasets/allenai/WildChat-4.8M/raw/main/README.md
- CSAM report thread (2024-07-19/24): https://huggingface.co/datasets/allenai/WildChat/discussions/4
- Trust No Bot, arXiv 2407.11438 v2: https://arxiv.org/html/2407.11438v2
- Developing Story / Breaking News (journalism), arXiv 2406.13706: https://arxiv.org/abs/2406.13706
- WildVis, arXiv 2409.03753: https://arxiv.org/html/2409.03753
- Can Prompt Anonymity Protect Your Identity From LLM Providers?, arXiv 2609.33903 (2026-09-27): https://arxiv.org/abs/2609.33903
- LMSYS-Chat-1M card: https://huggingface.co/datasets/lmsys/lmsys-chat-1m and paper: https://arxiv.org/abs/2309.11998
- Text Anonymization Benchmark, Computational Linguistics 48(4), Dec 2022: https://aclanthology.org/2022.cl-4.19/

*No dataset rows were downloaded for this case study.*
