// Stands in for the llm-credentials CLI in extension-host tests: answers profile show, token and repo show.
const args = process.argv.slice(2);
const gateway = process.env.LLM_CREDENTIALS_TEST_GATEWAY;
const fail = (message) => {
  process.stderr.write(`Error: ${message}\n`);
  process.exit(1);
};
if (args[0] === "profile" && args[1] === "show") {
  console.log(JSON.stringify({ name: "test", endpoints: { "openai-chat": `${gateway}/v1` } }));
} else if (args[0] === "token") {
  console.log(JSON.stringify({ access_token: "host-token", expires_at: null, profile: "test", user: "tester" }));
} else if (args[0] === "repo" && args[1] === "show") {
  const directory = args[args.indexOf("--directory") + 1];
  if (/unmapped/.test(directory)) fail(`No customer is assigned to folder ${directory}. Nothing was sent untagged.`);
  console.log(JSON.stringify({
    customer: "customer:host", source: "file", detail: "test", repository: null,
    headers: { "x-test-customer": "customer:host", "x-test-source": "file" },
  }));
} else {
  fail(`unexpected arguments ${args.join(" ")}`);
}
