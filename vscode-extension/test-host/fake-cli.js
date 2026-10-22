// Stands in for the vkm CLI in extension-host tests: answers profile show, token and repo show.
const args = process.argv.slice(2);
const gateway = process.env.VKM_TEST_GATEWAY;
const fail = (message) => {
  process.stderr.write(`Error: ${message}\n`);
  process.exit(1);
};
if (args[0] === "profile" && args[1] === "show") {
  console.log(JSON.stringify({ name: "test", endpoints: { "openai-chat": `${gateway}/v1` } }));
} else if (args[0] === "token") {
  console.log(JSON.stringify({ access_token: "host-token", expires_at: null, profile: "test", user: "tester" }));
} else if (args[0] === "repo" && args[1] === "show") {
  // The shape of `vkm repo show --json`, which exits 0 also while required labels are missing.
  const directory = args[args.indexOf("--directory") + 1];
  const missing = /unmapped/.test(directory);
  console.log(JSON.stringify({
    labels: [{
      key: "customer", value: missing ? null : "host", name: missing ? null : "Host", source: missing ? null : "file",
      detail: missing ? null : `${directory}/.vkm`, required: true, description: "Who the work is billed to",
    }],
    missing: missing ? ["customer"] : [], ignored: [], repository: null, complete: !missing,
    headers: missing ? {} : { "x-test-tags": "customer:host", "x-test-metadata": '{"labels":{"customer":"host"}}' },
    message: missing ? `Required labels have no value: customer for folder ${directory}. Nothing was sent.` : null,
    notices: [],
  }));
} else {
  fail(`unexpected arguments ${args.join(" ")}`);
}
