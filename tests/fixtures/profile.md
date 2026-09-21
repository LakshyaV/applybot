---
# ============================================================================
# MACHINE-READABLE FACTS. Scripts read this block; the prose below is for the LLM.
# `null` = NOT YET PROVIDED. The agent must never guess a null — it asks once and saves.
# ============================================================================
identity:
  first_name: Jane
  last_name: Doe
  full_name: Jane Doe
  preferred_name: Jane
  pronouns: null                      # optional; null → leave blank / "prefer not to say"
  email: jane.doe@example.edu
  phone_e164: "+15555550100"
  phone_display: "+1 555-555-0100"
  phone_country_code: "+1"
  phone_national: "5555550100"
  over_18: true

links:
  linkedin: https://www.linkedin.com/in/janedoe
  github: https://github.com/janedoe
  website: https://janedoe.example/
  portfolio: https://janedoe.example/

address:
  line1: null
  line2: null
  city: null
  province_state: null
  postal_code: null
  country: Canada

resume_path: tests/fixtures/resume.pdf

education:
  - school: University of Waterloo
    location: Waterloo, ON, Canada
    degree: Bachelor of Software Engineering (BSE)
    degree_level: "Bachelor's"
    major: Software Engineering
    start: null
    end: "2030-04"
    end_is_expected: true
    gpa: null                         # number + scale, or the string "do_not_disclose"
    currently_enrolled: true

# Truth values for the work-authorization question family, keyed by the JOB's country.
# The same question text ("Are you legally authorized to work here?") has different true
# answers on a Canadian and a US posting, so fillers must always resolve with job_country.
work_authorization:
  citizenships: [Canada]
  by_country:
    CA:
      authorized: true
      requires_sponsorship: false
    US:
      authorized: false
      requires_sponsorship: true
      note: "Canadian citizen. Eligible for TN status and the J-1 intern visa commonly used by Waterloo students."
  default:                            # any other country
    authorized: false
    requires_sponsorship: true
  security_clearance: none
  us_person_itar: false               # not a US citizen / permanent resident / protected individual

availability:
  season: Summer 2027
  earliest_start: "2027-05-03"
  latest_end: "2027-08-27"
  duration_weeks: 16
  full_time: true
  willing_to_relocate: true
  work_models: [onsite, hybrid, remote]

# Voluntary self-identification. "decline" → choose the form's "I don't wish to answer" option.
eeo:
  gender: decline
  race_ethnicity: decline
  hispanic_latino: decline
  veteran_status: decline
  disability: decline
  lgbtq: decline

history:
  criminal_conviction: false
  non_compete_or_restrictive_agreement: false
  relatives_at_company: null          # default answer when asked about the target company
  previously_employed_at: ["Initech Technologies", "Globex Corp."]
  previously_applied_default: null

defaults:
  how_did_you_hear: null
  salary_expectation: null            # e.g. "Open / negotiable"

consents:
  agent_may_certify_truthfulness: true
  agent_may_acknowledge_privacy_notice: true
  sms_marketing_opt_in: false
  talent_community_opt_in: false

# Never entered by the agent under any circumstances → human lane.
never_enter: [ssn, sin, date_of_birth, government_id, passport_number, bank_details]
---

# Synthetic test profile (same schema as profile/profile.md, no real data)
