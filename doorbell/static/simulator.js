const form = document.querySelector("#simulator");
const scenario = document.querySelector("#scenario");
const known = document.querySelector("#known");
const turns = document.querySelector("#turns");
const template = document.querySelector("#turn-template");
const add = document.querySelector("#add");
const events = document.querySelector("#events");
const status = document.querySelector("#status");
const empty = document.querySelector("#empty");
const submit = form.querySelector('[type="submit"]');

const scenarios = {
  delivery: [["I've got a package for you.", "DELIVERY"]],
  visitor: [["Hi, is anyone home?", "VISITOR"], ["It's Sam from next door.", "VISITOR"]],
  solicitor: [["Would you like to switch internet providers?", "SOLICITOR"], ["Can I leave a brochure?", "SOLICITOR"]],
  known: [["Hey, I'm home.", "VISITOR"]],
  silence: [["", "UNCLEAR"]],
  unclear: [["Um, is this the right place?", "UNCLEAR"]],
};

function updateTurns() {
  [...turns.children].forEach((turn, index) => {
    turn.querySelector("legend").textContent = `Listening turn ${index + 1}`;
    turn.querySelector(".remove").disabled = turns.children.length === 1;
    turn.disabled = known.checked;
  });
  add.disabled = known.checked || turns.children.length >= 3;
}

function appendTurn(transcript, intent) {
  const turn = template.content.firstElementChild.cloneNode(true);
  turn.querySelector("textarea").value = transcript;
  turn.querySelector("select").value = intent;
  turn.querySelector(".remove").addEventListener("click", () => {
    turn.remove();
    updateTurns();
    add.focus();
  });
  turns.append(turn);
  return turn;
}

function loadScenario() {
  known.checked = scenario.value === "known";
  turns.replaceChildren();
  scenarios[scenario.value].forEach(([transcript, intent]) => appendTurn(transcript, intent));
  updateTurns();
}

scenario.addEventListener("change", loadScenario);
known.addEventListener("change", updateTurns);
add.addEventListener("click", () => {
  const turn = appendTurn("", "UNCLEAR");
  updateTurns();
  turn.querySelector("textarea").focus();
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  submit.disabled = true;
  status.textContent = "Running";
  empty.hidden = true;
  events.replaceChildren();

  const payload = {
    known_face: known.checked,
    turns: [...turns.children].map((turn) => ({
      transcript: turn.querySelector("textarea").value,
      intent: turn.querySelector("select").value,
    })),
  };

  try {
    const response = await fetch("/api/simulate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "The simulation failed.");

    for (const event of result.events) {
      const item = document.createElement("li");
      const title = document.createElement("strong");
      const detail = document.createElement("div");
      title.textContent = event.state;
      detail.textContent = event.detail;
      item.append(title, detail);
      events.append(item);
    }
    status.textContent = result.status;
  } catch (error) {
    status.textContent = "Failed";
    empty.textContent = error instanceof SyntaxError ? "The simulator returned an unexpected response." : error.message;
    empty.hidden = false;
  } finally {
    submit.disabled = false;
  }
});

loadScenario();
