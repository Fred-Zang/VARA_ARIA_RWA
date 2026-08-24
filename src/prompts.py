# -*- coding: utf-8 -*-
"""Prompts publics de démonstration.

Les prompts de production et le framework réglementaire complet constituent une
partie du savoir-faire du projet et ne sont pas publiés. Les templates ci-dessous
préservent uniquement l'interface attendue par le code.
"""
from string import Template

prompt_template_agent_1 = Template(r"""
PUBLIC DEMO PROMPT — AGENT 1

Analyse le fragment documentaire fourni à partir des métriques reçues. Pour
chaque métrique, utilise son champ `llm_prompt` comme règle d'évaluation.
Retourne uniquement un tableau JSON. Chaque objet doit contenir :
`framework_id`, `flag`, `page_number`, `whitepaper_excerpt`, `output`.
Les listes associées à un même objet doivent rester alignées par index.

Métriques :
$metrics_block

Fragment paginé :
$whitepaper_chunk

Instructions complémentaires :
$instructions
""")

prompt_template_stage2_delete_redundancy = Template(r"""
PUBLIC DEMO PROMPT — AGENT 2

À partir de l'objet JSON ci-dessous, identifie seulement les éléments réellement
redondants. Retourne uniquement un objet JSON avec :
`kept_indices`, `deleted_indices`, `justifications`.
Les indices sont basés sur les listes de l'entrée reçue et commencent à 0.

Entrée :
$entry_json
""")

prompt_template_agent_3 = Template(r"""
PUBLIC DEMO PROMPT — AGENT 3

Produis une synthèse exécutive structurée à partir des résultats d'audit fournis.
Retourne uniquement du JSON avec deux sections : `critical_weakness` et
`strategic_recommendations`. Chaque section contient des listes `title` et
`description` de même longueur.

Données :
$json_data_to_analyse
""")
