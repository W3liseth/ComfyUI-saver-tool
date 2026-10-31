# ComfyUI-saving-tools

## Selective Image Saver

Node (catégorie `image`) qui affiche un aperçu de chaque image connectée et laisse choisir lesquelles enregistrer.

- **Entrées** : `image_1`, `image_2`, … Une nouvelle entrée apparaît dès que la dernière est connectée.
- **Sorties** : une sortie `image_N` par entrée connectée, qui renvoie l'image telle quelle pour continuer le workflow.
- **Nom** : format du nom des fichiers, commun à toutes les images du node (par défaut `ComfyUI_%counter%`) :
  - `%counter%` : numéro qui s'incrémente (`00001`, `00002`, …)
  - `%date%` ou `%date:yyyy-MM-dd hh-mm-ss%` : date et heure (`yyyy`, `yy`, `MM`, `dd`, `hh`, `mm`, `ss`)
  - `%rand8%` : 8 lettres ou chiffres aléatoires, différents pour chaque image (`%rand4%`, `%rand12%`, … pour une autre longueur, jusqu'à 999)
  - `%slot%` : nom de l'entrée (`image_1`, …)
  - `%batch%` : position de l'image dans le batch

  Si le nom existe déjà, un numéro est ajouté : seul **Écraser** remplace un fichier.
- **Par image** :
  - le **dossier** : un clic ouvre la fenêtre Windows « Sélectionner un dossier » (Windows uniquement, sur le PC où tourne ComfyUI).
  - **Enregistrer** : copie l'aperçu dans le dossier. Si aucun dossier n'est choisi, la fenêtre s'ouvre d'abord.
  - **Écraser** : remplace le dernier fichier enregistré depuis cette entrée par l'image actuelle.

Les PNG enregistrés contiennent le workflow, comme avec le node Save Image. Les chemins et le nom du dernier fichier enregistré sont sauvegardés avec le workflow. Les aperçus, eux, restent dans le dossier temporaire de ComfyUI : si ComfyUI a redémarré, relancez le workflow avant d'enregistrer.
