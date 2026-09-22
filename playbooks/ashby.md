# Ashby (jobs.ashbyhq.com) — scripted lane notes

- Schema: POST `https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting` with the query in
  `src/applybot/forms/ashby_posting.graphql` (header `apollographql-client-name: frontend_non_user`). Returns
  `applicationForm.sections[].fieldEntries[]` (`field.path/title/type/selectableValues`, `isRequired`) plus
  `surveyForms` (EEO, optional) and `employmentType`, `applicationLimitCalloutHtml`, `recaptchaAction`.
- Field types seen: String, Email, Phone (random path; identified by title "Phone"), File, Location (geocoder
  autocomplete, suggestion text "City, Region, Country"), ValueSelect (radio group), MultiValueSelect (checkbox
  group, checkbox `name` = option label), Boolean (Yes/No buttons `data-option`), LongText, Date (react-datepicker,
  accepts typed MM/DD/YYYY + Enter).
- DOM: `.ashby-application-form-field-entry[data-field-path=…]`; radio/checkbox groups are `<fieldset>`s whose
  `label.ashby-application-form-question-title[for=path]` names the path. Required = `_required_` class on that label.
- Submit: `button.ashby-application-form-submit-button`; invisible reCAPTCHA v3 (`recaptchaAction: job_apply`).
  Success = form gone + "thank you / application submitted" text. Never retry on an unidentified outcome.
