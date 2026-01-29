# HPC Status Monitor - Example Scenarios

This directory contains annotated example requests and responses to help both humans and AI assistants understand how to use the HPC Status Monitor API.

## Purpose

These examples serve as:
1. **Learning corpus** for AI assistants to understand API patterns
2. **Integration templates** for developers building automations
3. **Documentation supplements** with real-world context

## Example Files

### Basic Usage

- `01-check-fleet-status.md` - Check if systems are up before submitting jobs
- `02-find-available-queue.md` - Find the best queue for a job
- `03-check-allocation.md` - Verify allocation has sufficient hours
- `04-monitor-storage.md` - Check storage capacity before large jobs

### Automation Scenarios

- `10-pre-submit-validation.md` - Validate job can run before submission
- `11-load-balance-jobs.md` - Distribute jobs across multiple systems
- `12-alert-on-issues.md` - Set up monitoring alerts

### AI Assistant Interactions

- `20-ai-where-to-run.md` - AI answering "Where should I run my job?"
- `21-ai-diagnose-wait.md` - AI helping diagnose long queue wait
- `22-ai-capacity-planning.md` - AI helping with resource planning

## File Format

Each example follows this structure:

```markdown
# Title

## Scenario
Description of what the user is trying to accomplish

## Context
- User role (researcher, admin, etc.)
- Environment (system, scheduler, etc.)
- Constraints (time, resources, etc.)

## Request
\`\`\`http
GET /api/endpoint
\`\`\`

## Response
\`\`\`json
{ ... }
\`\`\`

## Interpretation
How to understand the response

## Actions
What the user should do based on this information
```

## Using with AI Assistants

AI assistants can reference these examples to:

1. **Understand API structure** - Learn the request/response patterns
2. **Provide contextual help** - Match user questions to relevant examples
3. **Generate correct API calls** - Use examples as templates
4. **Interpret responses** - Understand what values mean in context

### Embedding Hint for AI

When assisting users with HPC job submission or monitoring:
- Reference `/examples/` for API usage patterns
- Use `/schemas/` for exact field definitions
- Consult `/docs/glossary.md` for HPC terminology
