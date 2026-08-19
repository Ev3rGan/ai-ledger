# ADR 0008: Focused source portfolio

- Status: approved, staged across M1 and M2
- Scope: public daily Source Definitions and acquisition policy

## Context

The earlier active portfolio included AI Business even though reliable body acquisition could be
blocked, while 36kr existed only in research history and was never an approved active Profile.
The product needs a small bilingual mix with clear provenance, topic role, and access policy.

## Alternatives

- Keep every previously investigated publisher active for breadth.
- Add arbitrary source plugins and decide policy at collection time.
- Use a fixed eight-source portfolio plus one authorization-dependent entry.

## Decision

The approved target contains eight active Source Definitions:

1. Gemini API Release Notes
2. THE DECODER
3. TechCrunch AI
4. Hugging Face Blog
5. QbitAI
6. OpenAI News through the official News/RSS boundary
7. GitHub Trending as a Community Signal
8. Hugging Face Daily Papers through the official Hub Papers interface

Machine Heart is a ninth conditional definition and remains disabled until a formally authorized
data entry is available. AI Business is retired from active profiles, scheduling, status, and
site-specific runtime handling; 36kr remains excluded. M1 performs that retirement only. M2 owns
activation of missing approved entries and must not add sources outside this list.

Whitelisting permits a bounded acquisition attempt. It never permits login, paywall, CAPTCHA,
robots, consent, or anti-bot bypass. Community Signals cannot independently establish a Claim.

## Accepted tradeoff

The fixed portfolio sacrifices breadth and may leave a conditional Chinese source disabled. It
gains an auditable policy, predictable operations, and clearer evidence roles. Retired activation
evidence remains discoverable through the [historical index](../archive/README.md).

## Revisit trigger

Revisit a source only when its access authorization, stable entry point, body quality, provenance,
failure policy, and product value are documented in an approved ticket. A temporary feed response
or desire for broader coverage is not enough.
