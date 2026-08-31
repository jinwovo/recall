## Retrieval eval — bm25 vs vector vs hybrid

300 queries · doc-level ranking depth 10 · `http://localhost:18080`

Point estimate with its 95% interval — exact binomial where the per-query metric is 0/1, BCa bootstrap otherwise ([ADR 0011](docs/adr/0011-statistical-inference-eval.md)).

| mode | Recall@5 | Recall@10 | MRR@10 | nDCG@10 |
|---|:---:|:---:|:---:|:---:|
| bm25 | 0.725<br><sub>[0.67, 0.77]</sub> | 0.769<br><sub>[0.72, 0.81]</sub> | 0.639<br><sub>[0.59, 0.69]</sub> | 0.666<br><sub>[0.62, 0.71]</sub> |
| vector | 0.740<br><sub>[0.69, 0.79]</sub> | 0.788<br><sub>[0.74, 0.83]</sub> | 0.615<br><sub>[0.57, 0.66]</sub> | 0.650<br><sub>[0.61, 0.69]</sub> |
| hybrid | 0.802<br><sub>[0.76, 0.84]</sub> | 0.844<br><sub>[0.80, 0.88]</sub> | 0.704<br><sub>[0.66, 0.75]</sub> | 0.734<br><sub>[0.69, 0.77]</sub> |

### Significance vs `bm25`

Paired randomization test on per-query scores, Holm-corrected across the modes compared within each metric. `unresolvable` means the queries that differ are too few for *any* outcome to reach p < 0.05 — a sample-size fact, not a result.

| metric | mode | Δ | p | Holm p | verdict |
|---|---|:---:|:---:|:---:|---|
| recall@5 | `vector` | +0.015 | 0.5366 | 0.5366 | not significant |
| recall@5 | `hybrid` | +0.077 | 0.0003 | 0.0006 | ✅ significant |
| recall@10 | `vector` | +0.018 | 0.4104 | 0.4104 | not significant |
| recall@10 | `hybrid` | +0.075 | 0.0001 | 0.0002 | ✅ significant |
| mrr@10 | `vector` | -0.025 | 0.2464 | 0.2464 | not significant |
| mrr@10 | `hybrid` | +0.065 | 0.0005 | 0.0010 | ✅ significant |
| ndcg@10 | `vector` | -0.016 | 0.4111 | 0.4111 | not significant |
| ndcg@10 | `hybrid` | +0.068 | 0.0001 | 0.0002 | ✅ significant |

### What this gold set can resolve

`hybrid` vs `bm25` on **mrr@10**, n = 300 queries, per-query difference sd = 0.313.

- Smallest effect detectable at 80% power: **+0.051**
- Smallest two-sided p this comparison can return: **0.0000**
- Queries needed to resolve a +0.020 change: **1,918**

<details><summary>First relevant rank per query</summary>

| query | bm25 | vector | hybrid |
|---|:---:|:---:|:---:|
| 0-dimensional biomaterials show inductive prope… | — | — | — |
| All hematopoietic stem cells segregate their ch… | 1 | 2 | 1 |
| Radioiodine treatment of non-toxic multinodular… | 1 | 1 | 1 |
| Rapamycin decreases the concentration of triacy… | 1 | 1 | 1 |
| Rapid phosphotransfer rates govern fidelity in … | 2 | 2 | 2 |
| Rapid up-regulation and higher basal expression… | 2 | 2 | 2 |
| Rapid up-regulation and higher basal expression… | 2 | 2 | 3 |
| Recurrent mutations occur frequently within CTC… | 1 | 2 | 1 |
| Reduced responsiveness to interleukin-2 in regu… | 1 | 1 | 1 |
| Replacement of histone H2A with H2A.Z slows gen… | 5 | 6 | 7 |
| Ribosomopathies have a low degree of cell and t… | — | 5 | 5 |
| S-nitrosylated GAPDH physiologically transnitro… | 1 | 1 | 1 |
| Sildenafil improves erectile function in men wh… | 1 | 1 | 1 |
| Silencing of Bcl2 is important for the maintena… | 2 | 1 | 2 |
| Smc5/6 engagment drives the activation of SUMO … | 1 | 1 | 1 |
| Statins decrease blood cholesterol. | 3 | — | — |
| Statins increase blood cholesterol. | 2 | 4 | 5 |
| Stroke patients with prior use of direct oral a… | 1 | 1 | 1 |
| Subcutaneous fat depots undergo extensive brown… | 1 | 3 | 1 |
| Suboptimal nutrition is not predictive of chron… | — | 6 | 4 |
| Synaptic activity enhances local release of bra… | 2 | 4 | 1 |
| Angiotensin converting enzyme inhibitors are as… | 1 | 2 | 2 |
| T regulatory cells (tTregs) lacking αvβ8 are mo… | 1 | 1 | 1 |
| TCR/CD3 microdomains are a required to induce t… | 7 | 1 | 3 |
| TNFAIP3 is a tumor suppressor in glioblastoma. | 1 | 1 | 1 |
| Taking 400mg of α-tocopheryl acetate helps to p… | 1 | 1 | 1 |
| Taxation of sugar-sweetened beverages had no ef… | 1 | 1 | 1 |
| Teaching hospitals do not provide better care t… | 1 | 1 | 1 |
| Anthrax spores can be disposed of easily after … | 1 | 1 | 1 |
| Tetraspanin-3 is a causative factor in the deve… | 1 | 1 | 1 |
| The DdrB protein from Deinococcus radiodurans i… | 1 | 1 | 1 |
| The PPR MDA5 has two N-terminal CARD domains. | — | — | — |
| The PRR MDA5 has a central DExD/H RNA helices d… | — | — | — |
| Antibiotic induced alterations in the gut micro… | 1 | 1 | 1 |
| The PRR MDA5 is a sensor of RNA virus infection. | 5 | 1 | 3 |
| The US health care system can save up to $750 m… | 1 | 1 | 1 |
| The YAP1 and TEAD complex tanslocates into the … | 1 | 2 | 1 |
| The amount of publicly available DNA data doubl… | — | — | — |
| The arm density of TatAd complexes is due to st… | 8 | 1 | 1 |
| The availability of safe places to study is eff… | — | — | — |
| The availability of safe places to study is not… | — | 5 | 2 |
| The benefits of colchicine were achieved with e… | — | — | — |
| The binding orientation of the ML-SA1 activator… | — | — | — |
| The center of the granuloma in an immune cell i… | 3 | 2 | 1 |
| The combination of H3K4me3 and H3K79me2 is foun… | 1 | 1 | 1 |
| The composition of myosin-II isoform switches f… | 1 | 1 | 1 |
| The deregulated and prolonged activation of mon… | — | — | — |
| The extracellular domain of TMEM27 is cleaved i… | 1 | 1 | 1 |
| The genomic aberrations found in matasteses are… | — | 1 | 1 |
| The locus rs647161 is associated with colorecta… | 1 | 1 | 1 |
| The loss of the TET protein functions may have … | — | — | 7 |
| The minor G allele of FOXO3 is related to more … | 1 | 1 | 1 |
| Antiretroviral therapy reduces rates of tubercu… | 1 | 1 | 1 |
| The myocardial lineage develops from cardiac pr… | — | 1 | 1 |
| The one-child policy has been successful in low… | 3 | 2 | 2 |
| The relationship between a breast cancer patien… | 2 | 1 | 2 |
| The repair of Cas9-induced double strand breaks… | 1 | 1 | 1 |
| The risk of breast cancer among parous women in… | 2 | 1 | 1 |
| Arginine 90 in p150n is important for interacti… | 2 | 1 | 1 |
| The risk of male prisoners harming themselves i… | 1 | 1 | 1 |
| The severity of cardiac involvement in amyloido… | 1 | 1 | 1 |
| The single flash-evoked ERG b-wave is generated… | 4 | — | 3 |
| The sliding activity of kinesin-8 protein Kip3 … | 1 | 1 | 1 |
| The tip of the inner tube of the toxic type VI … | 2 | 1 | 1 |
| The treatment of cancer patients with co-IR blo… | — | — | — |
| The treatment of cancer patients with co-IR blo… | — | 5 | — |
| Arterioles have a larger lumen diameter than ve… | 1 | — | 1 |
| The ureABIEFGH gene cluster encodes urease matu… | — | — | 7 |
| The ureABIEFGH gene cluster is induced by nicke… | — | — | — |
| Therapeutic use of the drug Dapsone to treat py… | 1 | 1 | 1 |
| Articles published in open access format are le… | 1 | 1 | 1 |
| There is an inverse relationship between hip fr… | 1 | 1 | 1 |
| There is no association between HNF4A mutations… | 1 | 1 | 1 |
| Thigh-length graduated compression stockings (G… | 1 | 1 | 1 |
| 5% of perinatal mortality is due to low birth w… | — | — | — |
| Articles published in open access format are mo… | 1 | 1 | 1 |
| Tirasemtiv has no effect on fast-twitch muscle. | 1 | 3 | 9 |
| Transferred UCB T cells acquire a memory-like p… | — | — | — |
| Transplanted human glial cells can differentiat… | 4 | 2 | 1 |
| Aspirin inhibits the production of PGE2. | — | — | — |
| Transplanted human glial progenitor cells are i… | 1 | 1 | 1 |
| Assembly of invadopodia is triggered by focal g… | 7 | 1 | 1 |
| Tumor necrosis factor alpha (TNF-α) and interle… | — | — | — |
| UCB T cells maintain high TCR diversity after t… | 1 | 2 | 1 |
| UCB T cells reduce TCR diversity after transpla… | 1 | 2 | 1 |
| Ubiquitin ligase UBC13 generates a K63-linked p… | 1 | 4 | 2 |
| Ultrasound guidance significantly raises the nu… | 1 | 1 | 1 |
| Up-regulation of the p53 pathway and related mo… | 1 | 2 | 2 |
| Upregulation of mosGCTL-1 is induced upon infec… | 1 | 1 | 1 |
| Varenicline monotherapy is more effective after… | 1 | 1 | 2 |
| Venules have a larger lumen diameter than arter… | 1 | — | 2 |
| Venules have a thinner or absent smooth layer c… | 1 | — | 2 |
| Vitamin D deficiency effects the term of delive… | — | 1 | 1 |
| Asymptomatic visual impairment screening in eld… | 1 | 1 | 1 |
| Vitamin D deficiency is unrelated to birth weig… | — | 2 | 1 |
| Women with a higher birth weight are more likel… | 2 | 1 | 2 |
| aPKCz causes tumour enhancement by affecting gl… | — | 1 | 1 |
| cSMAC formation enhances weak ligand signalling. | 1 | 3 | 1 |
| mTORC2 regulates intracellular cysteine levels … | 1 | 1 | 1 |
| p16INK4A accumulation is  linked to an abnormal… | 1 | 2 | 2 |
| Auditory entrainment is strengthened when peopl… | 1 | 1 | 1 |
| Autologous transplantation of mesenchymal stem … | 1 | 1 | 1 |
| Autologous transplantation of mesenchymal stem … | 1 | 1 | 1 |
| Autologous transplantation of mesenchymal stem … | 1 | 1 | 1 |
| Autophagy declines in aged organisms. | 1 | 3 | 1 |
| Bariatric surgery has a positive impact on ment… | 1 | 1 | 1 |
| Basophils counteract disease development in pat… | 1 | 1 | 1 |
| Birth-weight is positively associated with brea… | 2 | 1 | 1 |
| Blocking the interaction between TDP-43 and res… | 1 | 1 | 1 |
| Bone marrow cells contribute to adult macrophag… | — | 8 | 1 |
| Breast cancer development is determined exclusi… | 2 | 6 | 1 |
| CCL19 is absent within dLNs. | 1 | — | 2 |
| CHEK2 is not associated with breast cancer. | 1 | 1 | 1 |
| CR is associated with higher methylation age. | 4 | 5 | 2 |
| CRP is not predictive of postoperative mortalit… | 4 | 4 | 5 |
| CX3CR1 on the Th2 cells impairs T cell survival | 1 | 1 | 1 |
| CX3CR1 on the Th2 cells promotes T cell survival | 1 | 1 | 1 |
| CX3CR1 on the Th2 cells promotes airway inflamm… | 1 | 1 | 1 |
| CX3CR1 on the Th2 cells suppresses airway infla… | 1 | 1 | 1 |
| Carriers of the alcohol aldehyde dehydrogenase … | 1 | 2 | 1 |
| Cataract and trachoma are the primary cause of … | 2 | 1 | 3 |
| Cell autonomous sex determination in somatic ce… | 1 | 1 | 1 |
| Cell autonomous sex determination in somatic ce… | 1 | 1 | 1 |
| Cells lacking clpC have a defect in sporulation… | 2 | 3 | 1 |
| Cells undergoing methionine restriction may act… | — | 4 | 3 |
| Cellular aging closely links to an older appear… | — | 1 | — |
| Chenodeosycholic acid treatment increases whole… | 1 | 1 | 1 |
| Chenodeosycholic acid treatment reduces whole-b… | 2 | 1 | 1 |
| Chronic aerobic exercise alters endothelial fun… | 6 | 5 | 2 |
| Cold exposure increases BAT recruitment. | 1 | 2 | 1 |
| Cold exposure reduces BAT recruitment. | 2 | 2 | 1 |
| Combination nicotine replacement therapies with… | 1 | 1 | 1 |
| Combining phosphatidylinositide 3-kinase and ME… | 1 | 2 | 2 |
| Commelina yellow mottle virus' (ComYMV) genome … | 1 | 1 | 1 |
| Crossover hot spots are not found within gene p… | 1 | 6 | 1 |
| Crosstalk between dendritic cells (DCs) and inn… | 1 | 4 | 1 |
| Cytochrome c is released from the mitochondrial… | 2 | 2 | 1 |
| 1,000 genomes project enables mapping of geneti… | 2 | 6 | 5 |
| Cytosolic proteins bind to iron-responsive elem… | 4 | — | — |
| DMRT1 is a sex-determining gene that is epigene… | 2 | — | — |
| De novo assembly of sequence data has more spec… | 4 | — | 3 |
| Deamination of cytidine to uridine on the minus… | 3 | — | 2 |
| Deleting Raptor reduces G-CSF levels. | 6 | — | 4 |
| Deletion of αvβ8 does not result in a spontaneo… | 1 | 1 | 1 |
| Dexamethasone decreases risk of postoperative b… | 1 | 1 | 1 |
| Diabetic patients with acute coronary syndrome … | 1 | 1 | 4 |
| Discrimination between the initiator and elonga… | 1 | 1 | 1 |
| Downregulation and mislocalization of Scribble … | 1 | 1 | 1 |
| A deficiency of vitamin B12 increases blood lev… | — | 7 | 8 |
| During the primary early antibody response acti… | 1 | 1 | 2 |
| Enhanced early production of inflammatory chemo… | 1 | 1 | 1 |
| Epidemiological disease burden from noncommunic… | — | 3 | 5 |
| Epigenetic modulating agents (EMAs) modulate an… | 3 | — | 2 |
| Errors in peripheral IV drug administration are… | 1 | 1 | 1 |
| Ethanol stress decreases the expression of IBP … | 3 | 1 | 4 |
| Exposure to fine particulate air pollution is r… | 1 | 1 | 1 |
| Febrile seizures increase the threshold for dev… | 1 | 1 | 1 |
| Febrile seizures reduce the threshold for devel… | 1 | 1 | 1 |
| Female carriers of the Apolipoprotein E4 (APOE4… | 5 | 1 | 1 |
| A high microerythrocyte count raises vulnerabil… | 1 | 1 | 1 |
| Flexible molecules experience greater steric hi… | — | — | — |
| FoxO3a activation in neuronal death is mediated… | — | — | — |
| Free histones are degraded by a Rad53-dependent… | 1 | 1 | 1 |
| Functional consequences of genomic alterations … | — | — | — |
| Fz/PCP-dependent Pk localizes to the anterior m… | 2 | 1 | 1 |
| Fz/PCP-dependent Pk localizes to the anterior m… | 2 | 1 | 1 |
| GATA-3 is important for hematopoietic stem cell… | 1 | 1 | 1 |
| Gene expression does not vary appreciably acros… | 7 | 7 | 2 |
| Glycolysis is one of the primary glycometabolic… | — | — | — |
| Golli-deficient T-cells prefer to differentiate… | 2 | — | 1 |
| A total of 1,000 people in the UK are asymptoma… | 6 | 2 | 1 |
| ADAR1 binds to Dicer to cleave pre-miRNA. | 1 | 1 | 1 |
| HNF4A mutations can cause diabetes in mutant ca… | 1 | 1 | 1 |
| 1/2000 in UK have abnormal PrP positivity. | 1 | 1 | 1 |
| AIRE is expressed in some skin tumors. | 1 | 1 | 1 |
| Headaches are not correlated with cognitive imp… | — | 2 | 1 |
| Healthcare delivery efficiency in crowded deliv… | — | — | — |
| Helminths interfere with immune system control … | 1 | 3 | 2 |
| Hematopoietic Stem Cell purification reaches pu… | — | — | — |
| ALDH1 expression is associated with better brea… | 1 | 1 | 1 |
| High cardiopulmonary fitness causes increased m… | 2 | 5 | 9 |
| High dietary calcium intakes are unnecessary fo… | — | 2 | 1 |
| High levels of CRP reduces the risk of exacerba… | 1 | 1 | 1 |
| High levels of copeptin decrease risk of diabet… | — | — | — |
| High-sensitivity cardiac troponin T (HSCT-T) do… | 1 | 3 | 1 |
| Histone demethylase recruitment and a transient… | 1 | 1 | 1 |
| Homozygous deletion of murine Sbds gene from os… | 2 | 3 | 1 |
| Human T-lymphotropic virus type-I-associated my… | 1 | 1 | 1 |
| ALDH1 expression is associated with poorer prog… | 1 | 1 | 1 |
| Hyperfibrinogenemia decreases rates of femoropo… | 1 | 9 | 2 |
| Hyperfibrinogenemia increases rates of femoropo… | 1 | 1 | 1 |
| Hypertension is frequently observed in type 1 d… | — | — | — |
| Hypocretin neurones induce panicprone state in … | 1 | 1 | 1 |
| Hypoglycemia increases the risk of dementia. | 1 | 1 | 1 |
| AMP-activated protein kinase (AMPK) activation … | 8 | 4 | 1 |
| Hypothalamic glutamate neurotransmission is cru… | 7 | 1 | 7 |
| IFIT1 restricts viral replication by sequestrat… | — | 5 | — |
| IRG1 has antiviral effects against neurotropic … | 1 | 3 | 1 |
| ITAM phosphorylation prevents the transfer of t… | 6 | 8 | 3 |
| IgA plasma cells that are specific for transglu… | 1 | 1 | 1 |
| Immune complex triggered cell death leads to ex… | 6 | 6 | 10 |
| APOE4 expression in iPSC-derived neurons increa… | 1 | 1 | 1 |
| Immune responses result in the development of i… | — | — | — |
| In adult tissue, most T cells are memory T cell… | 1 | 1 | 2 |
| APOE4 expression in iPSC-derived neurons increa… | 1 | 1 | 1 |
| In domesticated populations of Saccharomyces ce… | — | 1 | 1 |
| In mice, P. chabaudi parasites are able to prol… | — | 4 | 6 |
| In mouse models, the loss of CSF1R facilitates … | 1 | 1 | 1 |
| In transgenic mice harboring green florescent p… | 5 | 2 | 3 |
| In young and middle-aged adults, current or rem… | 1 | 1 | 1 |
| Incidence of heart failure decreased by 10% in … | 1 | 1 | 1 |
| Incidence rates of cervical cancer have decreas… | 1 | 1 | 1 |
| Incidence rates of cervical cancer have increas… | 3 | — | — |
| Increased microtubule acetylation repairs LRRK2… | 1 | 1 | 1 |
| Increased vessel density along with a reduction… | 6 | — | 4 |
| Individuals with low serum vitamin D concentrat… | — | 4 | — |
| Infection of human T-cell lymphotropic virus ty… | 2 | 1 | 1 |
| Inositol lipid 3-phosphatase PTEN converts Ptdl… | 2 | 1 | 1 |
| Input from  mental and physical health care pro… | — | — | 2 |
| Insomnia can be effectively treated with cognit… | 1 | 1 | 1 |
| Insulin increases risk of severe kidney failure. | 1 | 8 | 1 |
| Integrating classroom-based collaborative learn… | 3 | 3 | 4 |
| Ivermectin is used to treat lymphatic filariasi… | 1 | — | 2 |
| Ivermectin is used to treat onchocerciasis. | 2 | — | 1 |
| LDL cholesterol has no involvement in the devel… | 1 | 3 | 1 |
| Lack of clpC does not affect sporulation effici… | 1 | 3 | 4 |
| Less than 10% of the gabonese children with Sch… | — | 2 | 3 |
| Leukemia associated Rho guanine nucleotide-exch… | 1 | 1 | 1 |
| Leuko-increased blood increases infectious comp… | 1 | 2 | 1 |
| Leuko-reduced blood reduces infectious complica… | 1 | 1 | 1 |
| Activation of PPM1D suppresses p53 function. | 1 | 6 | 1 |
| Localization of PIN1 in the Arabidopsis embryo … | 1 | 1 | 2 |
| Localization of PIN1 in the roots of Arabidopsi… | 1 | 1 | 2 |
| Low expression of miR7a does represses target g… | 7 | — | 3 |
| Low expression of miR7a exerts a biological fun… | — | — | — |
| Low nucleosome occupancy correlates with low me… | 2 | 1 | 2 |
| Activator-inhibitor pairs are provided dorsally… | — | 2 | 1 |
| Lupus-prone mice infected with curliproducing b… | 1 | 1 | 1 |
| Ly49Q directs the organization of neutrophil mi… | 1 | 1 | 1 |
| Ly6C hi monocytes have a lower inflammatory cap… | 2 | 3 | 6 |
| Ly6C hi monocytes have a lower inflammatory cap… | 1 | 1 | 1 |
| Lymphadenopathy is observed in knockin mouse la… | 1 | 2 | 1 |
| Macrolides have no protective effect against my… | 1 | 1 | 1 |
| Macrolides protect against myocardial infarctio… | 1 | 5 | 1 |
| Macropinocytosis contributes to a cell's supply… | 2 | 3 | 3 |
| Active H. pylori urease has a polymeric structu… | 2 | 1 | 1 |
| Many proteins in human cells can be post-transl… | 8 | 2 | 3 |
| Mathematical models predict that using Artemisi… | 1 | 1 | 1 |
| Mercaptopurine is anabolized into the inactive … | 9 | — | 4 |
| Metastatic colorectal cancer treated with a sin… | 1 | 1 | 1 |
| Mice defective for deoxyribonucleic acid (DNA) … | — | — | — |
| Mice that lack Interferon-γ or its receptor exh… | 1 | 1 | 1 |
| Mice without IFN-γ or its receptor are resistan… | — | — | — |
| MicroRNA is involved in the regulation of Neura… | 2 | 3 | 1 |
| Microarray results from culture-amplified mixtu… | 2 | 1 | 1 |
| Mitochondria are uninvolved in apoptosis. | 1 | 2 | 1 |
| Modifying the epigenome in the brain affects th… | — | 3 | 2 |
| Monoclonal antibody targeting of N-cadherin inh… | 1 | 1 | 1 |
| Most termination events in Okazaki fragments ar… | 1 | 1 | 1 |
| Mutant mice lacking SVCT2 have greatly increase… | 1 | 1 | 1 |
| Mutations in G-Beta protein GNB2 are present in… | 1 | 1 | 1 |
| N-terminal cleavage increases success identifyi… | — | — | — |
| N-terminal cleavage reduces success identifying… | — | — | — |
| N348I mutations cause resistance to zidovudine … | 1 | 1 | 1 |
| NF2 (Merlin) causes phosphorylation and subsequ… | — | — | 9 |
| NF2 (Merlin) prevents phosphorylation and subse… | — | — | — |
| NFAT4 activation requires IP3R-mediated Ca2+ mo… | 1 | 1 | 1 |
| NOX2-independent pathways can generate peroxyni… | — | — | — |
| NR5A2 is important in development of endometria… | 1 | 1 | 1 |
| Nanoparticles can be targeted against specific … | 1 | 2 | 1 |
| Neutrophil extracellular traps (NETs) are relea… | 1 | 7 | 1 |
| New drugs for tuberculosis often do not penetra… | 1 | 3 | 1 |
| Non-invasive ventilation use should be decrease… | 1 | 3 | 1 |
| Normal expression of RUNX1 has tumor-promoting … | 3 | 3 | 2 |
| Obesity decreases life quality. | — | 5 | — |
| Obesity is determined solely by environmental f… | — | 2 | 1 |
| Occupancy of ribosomes by IncRNAs do not make f… | 1 | 1 | 1 |
| Occupancy of ribosomes by IncRNAs mirror 5 0-UT… | 1 | 1 | 1 |
| Omnivores produce less trimethylamine N-oxide f… | 1 | 1 | 1 |
| Only a minority of cells survive development af… | — | — | — |
| PD-1 triggering on monocytes reduces IL-10 prod… | 1 | 1 | 1 |
| PDPN promotes efficient motility along stromal … | 1 | 1 | 1 |
| PGE 2 promotes intestinal tumor growth by alter… | 1 | 1 | 1 |
| PKG-la plays an essential role in expression of… | 1 | 1 | 1 |
| PPAR-RXRs are inhibited by PPAR ligands. | — | — | — |
| PPAR-RXRs can be activated by PPAR ligands. | — | — | — |
| Participating in six months of physical activit… | 2 | 1 | 1 |
| Patients in stable partnerships have a faster p… | 1 | 1 | 1 |
| Peroxynitrite is required for nitration of TCR/… | 1 | 1 | 1 |
| Albendazole is used to treat lymphatic filarias… | 1 | — | 1 |
| Pleiotropic coupling of GLP-1R to intracellular… | 3 | 3 | 2 |
| Podocytes are motile and migrate in the presenc… | 2 | 1 | 1 |
| Polymeal nutrition reduces cardiovascular morta… | 1 | 1 | 1 |
| Pretreatment with the Arp2/3 inhibitor CK-666 a… | 1 | 1 | 1 |
| Primary cervical cancer screening with HPV dete… | 1 | 1 | 2 |
| Primary pro-inflammatory cytokines induce secon… | — | — | — |
| Proteins synthesized at the growth cone are ubi… | 1 | 3 | 1 |
| Pseudogene PTENP1 regulates the expression of P… | 1 | 1 | 1 |
| Alizarin forms hydrogen bonds with residues inv… | 1 | — | 1 |
| Pyridostatin destabilizes the G - quadruplex in… | 1 | 1 | 2 |

</details>
