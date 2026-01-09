## MicrogridUP

MicrogridUP is planning software that quickly identifies detailed microgrid investment options across a distribution system to improve resilience for critical facilities. The software uses distribution data utilities commonly use in their day-to-day operations to calculate a cost-optimal set of microgrids, and provides detailed analysis of generation mix, interconnection requirements, distribution upgrades, control characteristics, and backup survivability of the proposed systems. The ultimate goal of the software is to reduce the planning costs for microgrid deployments, which are currently much higher than other hardware deployments on the distribution system.

![Screenshot 2023-06-12 at 11 21 51 AM](https://github.com/dpinney/microgridup/assets/2131438/234b01c3-04ab-4b14-8fd2-b5debc74d502)

## Installation

1. Install [Docker](https://docs.docker.com/get-docker/)
1. Get the app `docker pull ghcr.io/nreca-bts/microgridup:main`
1. Create the container and start it with `docker run -d -p 5001:5000 --name mgucont ghcr.io/nreca-bts/microgridup:main`
1. The web app will then be running at http://127.0.0.1:5001
1. You can stop/start the app via `docker stop mgucont`/`docker start mgucont`

## Background

This software was developed through a 3-year research effort started in 2022 and funded by the Department of Defense's Environmental Security Technology Certification Program (ESTCP) under award number EW20-5055. The effort focuses on electric cooperatives that hold utility privatization (UP) contracts to own and operate the electrical infrastructure on US military installations. These installations have stringent requirements for energy resilience, e.g. the Army requirement for 14 days of islanded operation of all critical electric loads [1]. The majority of electric power outages at military installations have been due to disruptions to the bulk grid [1], so local microgrids are a potential solution; however, planning them is challenging (e.g. 20-40% of total project costs spent on planning) [2]. New microgrids must be deployed into a complex environment of legacy generators [3] and distribution infrastructure, there is no standard approach in the electric utility industry or DoD for microgrid planning on multi-building sites, and there is no comprehensive tool available that combines utility input data preparation, distribution analysis, generator sizing, and financial modeling.

Because financial and engineering design isn’t the only consideration when planning microgrids, we have also developed the MicrogridUP Planning Playbook [4], which covers a number of additional aspects of microgrid planning, which include:
-	Processes for gathering requirements from microgrid users.
-	Overviews of hardware and services available in the market.
-	Supply chain and software bill of materials (SBOM) requirements.
-	Commissioning and testing process.
-	Contracting and financial options.
-	Running the RFP process to acquire the microgrid assets.
-	Operations and Maintenance (O&M) of the assets.

## How it Works

Our microgrid planning process, in the order we perform it, goes as follows:
- Data Import – Import comprehensive data sets from distribution operators to give a full picture of historical reliability and costs across tens of thousands of components.
- Network Segmentation – Segment the distribution network automatically, weighted by load criticality, to find sets of maximum impact and mutually beneficial microgrid options.
- Distribution Design – Add distribution upgrades to the system model to determine cost impacts and run automated interconnection to confirm nothing exceeds hosting capacity.
- Generation Planning – Determine resilient and cost-optimal generation mixes of solar, wind, natural gas, energy storage and diesel for all candidate microgrids that satisfy their requirements for outage survival.
- System Control – Execute detailed control simulations to determine load, generation, switching and protection changes needed to safely island/de-island and black start. 
- Resilience and Financial Reports – Calculate detailed costs for each microgrid and a resilience summary explaining the survival characteristics during outages.

To illustrate what the results look like, we have created an artificial test system called “Lehigh Air Force Base” which has realistic mission, load, and distribution characteristics based on public data. Below are the final summary outputs.

![Screenshot 2023-06-12 at 11 22 02 AM](https://github.com/dpinney/microgridup/assets/2131438/4bfe29dd-9d26-4208-9809-31f8e10d334a)

The software has identified 4 potential microgrids (mg1, mg2, …, mg4 each circles in orange) to cover the critical loads and compared each of those microgrids to a single, large, centralized microgrid. The outage survival of each microgrid meets the minimum requirements of the critical loads they serve (set to 48 hours) while also minimizing cost through a combination of fossil, solar and energy storage assets. In some cases, the load factor of the critical meters is such that the average outage survival is far longer than the minimal requirement even though the generation mix is optimized to achieve the minimum survival requirement—a nice side effect. The central microgrid is predicted to have higher net present value than building all the smaller microgrids; however, the capital expenditure to build this large microgrid is naturally about four times that of each microgrid individually, so the distribution operator could pursue a staged deployment if total resources don’t meet the capital requirements of the large microgrid.

For a more detailed overview of the components of the software and how they operate, please see the documentation in section 4 of the MicrogridUP Planning Playbook [4].

## Project Partners 

We have developed this software in partnership with three cooperatives that serve four major military installations as shown in the map below. As of 2023, the software has been used to run planning exercises at each installation, and 4 microgrids identified by the software have had funding identified and are moving into deployment. The project team is interested in running similar planning exercise with additional utilities and military installations starting in 2024.

![Screenshot 2023-06-12 at 11 22 12 AM](https://github.com/dpinney/microgridup/assets/2131438/de3bfaed-9c89-48c9-a638-efe764b83bc4)

## References and Resources
-	[1] “Department of Defense Annual Energy Management and Resilience Report (AEMRR),” Fiscal Year 2018
-	[2] “Phase I Microgrid Cost Study: Data Collection and Analysis of Microgrid Costs in the United States,” Giraldez et al., NREL, 2018
-	[3] “Power Begins at Home: Assured Energy for US Military Bases,” Noblis commissioned by the PEW Charitable Trust, 2017
- [4] The MicrogridUP Planning Playbook (coming soon).
