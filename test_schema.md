// output/test_case_example.json
{
  "test_name": "Login Functionality Test",
  "feature_description": "User logs in with valid credentials and verifies the welcome message.",
  "recorded_at": "2023-10-27T10:00:00Z",
  "steps": [
    {
      "step_id": 1,
      "action": "navigate",
      "description": "Navigate to the login page", // Natural language
      "parameters": {
        "url": "https://practicetestautomation.com/practice-test-login/"
      },
      "selector": null, // Not applicable
      "wait_after_secs": 1.0 // Optional: Simple wait after action
    },
    {
      "step_id": 2,
      "action": "type",
      "description": "Type username 'student'",
      "parameters": {
        "text": "student",
        "parameter_name": "username" // Optional: For parameterization
      },
      "selector": "#username", // Recorded robust selector
      "wait_after_secs": 0.5
    },
    {
      "step_id": 3,
      "action": "type",
      "description": "Type password 'Password123'",
      "parameters": {
        "text": "Password123",
        "parameter_name": "password" // Optional: For parameterization
      },
      "selector": "input[name='password']",
      "wait_after_secs": 0.5
    },
    {
      "step_id": 4,
      "action": "click",
      "description": "Click the submit button",
      "parameters": {},
      "selector": "button#submit",
      "wait_after_secs": 1.0 // Longer wait after potential navigation/update
    },
    {
      "step_id": 5,
      "action": "wait_for_load_state", // Explicit wait example
      "description": "Wait for page load after submit",
      "parameters": {
        "state": "domcontentloaded" // Or "load", "networkidle"
      },
      "selector": null,
      "wait_after_secs": 0
    },
    {
      "step_id": 6,
      "action": "assert_text_contains",
      "description": "Verify success message is shown",
      "parameters": {
        "expected_text": "Congratulations student. You successfully logged in!"
      },
      "selector": "div.post-content p strong", // Selector for the element containing the text
      "wait_after_secs": 0
    },
    {
      "step_id": 7,
      "action": "assert_visible",
      "description": "Verify logout button is visible",
      "parameters": {},
      "selector": "a.wp-block-button__link:has-text('Log out')",
      "wait_after_secs": 0
    }
    // ... more steps
  ]
}