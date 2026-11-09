document.querySelectorAll("[data-choice-dropdown]").forEach((dropdown) => {
  const search = dropdown.querySelector("[data-choice-search]");
  const choices = [...dropdown.querySelectorAll("[data-choice]")];
  const choiceList = dropdown.querySelector("[data-choice-list]");
  const summary = dropdown.querySelector("[data-selection-summary]");
  const noChoices = dropdown.querySelector("[data-no-choices]");
  const isSingleSelect = dropdown.dataset.selectionMode === "single";

  choices.forEach((choice, index) => {
    choice.dataset.originalOrder = index;
  });

  const normalize = (value) => value
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase("sl");

  const updateSummary = () => {
    const selected = choices.filter((choice) => choice.querySelector("input").checked);
    if (selected.length === 0) {
      summary.textContent = dropdown.dataset.emptyLabel;
    } else if (isSingleSelect || selected.length === 1) {
      summary.textContent = selected[0].querySelector("span").textContent;
    } else {
      summary.textContent = `${selected.length} izbranih krajev`;
    }
  };

  const filterChoices = () => {
    const query = normalize(search.value.trim());
    let visible = 0;
    choices.forEach((choice) => {
      const matches = normalize(choice.dataset.searchText).includes(query);
      choice.hidden = !matches;
      if (matches) visible += 1;
    });
    noChoices.hidden = visible !== 0;
  };

  const sortChoices = () => {
    [...choices]
      .sort((left, right) => {
        const selectedDifference = Number(right.querySelector("input").checked)
          - Number(left.querySelector("input").checked);
        return selectedDifference || Number(left.dataset.originalOrder) - Number(right.dataset.originalOrder);
      })
      .forEach((choice) => choiceList.insertBefore(choice, noChoices));
  };

  choices.forEach((choice) => choice.querySelector("input").addEventListener("change", () => {
    sortChoices();
    updateSummary();
    if (isSingleSelect) {
      dropdown.removeAttribute("open");
      dropdown.querySelector("summary").focus();
    }
  }));
  search.addEventListener("input", filterChoices);
  dropdown.addEventListener("toggle", () => {
    if (dropdown.open) {
      sortChoices();
      search.value = "";
      filterChoices();
      search.focus();
    }
  });
  dropdown.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      dropdown.removeAttribute("open");
      dropdown.querySelector("summary").focus();
    }
  });
  document.addEventListener("click", (event) => {
    if (!dropdown.contains(event.target)) dropdown.removeAttribute("open");
  });

  sortChoices();
  updateSummary();
});
