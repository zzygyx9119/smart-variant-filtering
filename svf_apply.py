"""
Usage:
    svf_apply.py (--vcf STR) [--table STR] (--indel_model STR) (--snv_model STR) [--features_snv STR] [--features_indel STR] [--discard_existing_filters] [--keep_database_variants] [--verbose]

Description:
    Apply Smart Variant Filtering (SVF) traing models on input VCF

Arguments:
    --vcf STR                       Input VCF file
    --table STR                     Table with categorized variants, same as in VCF used for validation
    --indel_model STR               Path to the classification model for indels
    --snv_model STR                 Path to the classification model for SNVs
    --discard_existing_filters      Discard filters found in VCF [Default: False]
    --keep_database_variants           Keep all called variants that exist in dbsnp
    --features_snv STR        Comma separated list of features used for SNVs [default: QD,MQ,FS,MQRankSum,ReadPosRankSum,SOR]
    --features_indel STR      Comma separated list of features used for indels [default: QD,MQ,FS,MQRankSum,ReadPosRankSum,SOR]

Options:
    -h, --help                      Show this help message and exit.
    -v, --version                   Show version and exit.
    --verbose                       Log output [default: 0]

Examples:
    python svf_apply.py --vcf <raw_vcf> --indel_model indels.sav --snv_model SNVs.sav
    python svf_apply.py --snv_model data/wes/6_features/HG001_NIST7035_dbsnp_SNVs.snv.sav --indel_model data/wes/6_features/HG001_NIST7035_dbsnp_indels.indel.sav --vcf data/wes/6_features/HG005_oslo_exome_chr20.vcf
    python svf_apply.py --snv_model data/wes/6_features/HG001_NIST7035_dbsnp_SNVs.snv.sav --indel_model data/wes/6_features/HG001_NIST7035_dbsnp_indels.indel.sav --vcf data/wes/6_features/HG005_oslo_exome_chr20.vcf --discard_existing_filters

"""

# Load libraries
import pandas
from pandas.tools.plotting import scatter_matrix
from sklearn import model_selection
from sklearn.metrics import classification_report
from sklearn.metrics import confusion_matrix
from sklearn.metrics import accuracy_score
from sklearn.linear_model import LogisticRegression
from sklearn.tree import DecisionTreeClassifier
from sklearn.neighbors import KNeighborsClassifier
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis
from sklearn.naive_bayes import GaussianNB
from sklearn.svm import SVC
import pickle
import numpy as np
from docopt import docopt

def vprint(out, verbose):
    if verbose:
        print(out)

args = docopt(__doc__, version='1.0')
verbose = args['--verbose']
vcf = args['--vcf']
validation_table = args['--table']#'small.table'#'HG005_oslo_exome_raw_conf.table'#args['--table']
discard_existing_filters = args['--discard_existing_filters']
keep_database_variants = args['--keep_database_variants']
#Optional read of validation table to pandas dataframe
if validation_table:
    df_truth = pandas.read_csv(validation_table, sep='\t')
    df_truth.__delitem__('CHROM')
    df_truth.__delitem__('POS')
    df_truth.__delitem__('TYPE')
    params_line_all = np.zeros([100000, 7], dtype = float)

# load the model from disk
fname_model_snv = args['--snv_model']
loaded_model_snv = pickle.load(open(fname_model_snv, 'rb'))
fname_model_indel = args['--indel_model']
loaded_model_indel = pickle.load(open(fname_model_indel, 'rb'))

# Parse file input name and create output name
filename = vcf.split('/').pop()
basename = '.'.join(filename.split('.')[0:-1])
out_name = basename + '.svf.vcf'

features_snv = args['--features_snv'].split(',')
features_indel = args['--features_indel'].split(',')


# fields = ['QD', 'MQ', 'FS',	'MQRankSum', 'ReadPosRankSum', 'SOR', 'dbSNPBuildID']
# fields = fields[0:num_features]

ep = {} # Extracted parameters from VCF

filter_line = '##FILTER=<ID=SVF_SNV,Description="Smart variant filtering for SNVs">\n'
filter_line += '##FILTER=<ID=SVF_INDEL,Description="Smart variant filtering for indels">\n'
filter_written = False
tp = 0
fp = 0
unclassified = 0
y_predictions = []
ln_cnt = 0
not_pres = {}
for ff in set(features_snv + features_indel):
    not_pres[ff] = 0

with open(vcf, 'r') as main, open(out_name, 'w') as out:
    for line in main:
        if line.startswith('#'):
            if (line.find('FILTER=<ID=') >= 0 or line.find('CHROM\tPOS') >= 0) and filter_written == False:
                out.write(filter_line)
                filter_written = True

            out.write(line)
        else:
            ln_cnt += 1
            parts = line.split('\t')
            REF = parts[3]
            ALT = parts[4]
            INFO = parts[7]
            if ',' in ALT:
                if len(ALT.split(',')[0]) == len(ALT.split(',')[1]):
                    alt_size = len(ALT.split(',')[0]) #max(ALT.split(','), key=len)
                    is_snv = len(REF) == 1 and len(REF) == alt_size
                else:
                    is_snv = False
            else:
                is_snv = len(REF) == 1 and len(REF.replace('.','')) == len(ALT.replace('.',''))
            # Collect necessary params from VCF INFO field
            if is_snv:
                num_features = len(features_snv)
                fields = features_snv
            else:
                num_features = len(features_indel)
                fields = features_indel

            params_line = [0.] * num_features
            cnt = 0
            for f in fields:
                feature_found = False
                for info_field in INFO.split(';'):
                    if info_field.split('=')[0] == f:
                        feature_val = info_field.split('=')[1]
                        params_line[cnt] = (float(feature_val))
                        break  # Feature found - exit the inner loop
                if f == 'dbSNPBuildID' and params_line[cnt] > 0.:
                    params_line[cnt] = 1.0  # Discard info about dbsnp revision
                cnt += 1
            params_line = np.ndarray((1,len(fields)), buffer=np.array(params_line), dtype=float)

            # print("--- %s ms --- 2. Prepare features from VCF " % (1000 * (time.time() - start_time)))
            #start_time = time.time()
            if keep_database_variants:
                variant_present_in_database = False
                for info_field in INFO.split(';'):
                    if info_field.split('=')[0] == 'dbSNPBuildID':
                        variant_present_in_database = True
                        break  # database presence found - exit loop
            # Perform prediction (classification)
            if keep_database_variants and variant_present_in_database:
                prediction = ['TP']
            elif is_snv:
                prediction = loaded_model_snv.predict(params_line)
            else:
                prediction = loaded_model_indel.predict(params_line)
            y_predictions.append(str(prediction[0]))

            if prediction == ['FP']:
                # Mark current line as filtered
                parts[6] = 'SVF_SNV' if is_snv else 'SVF_INDEL'
                line = '\t'.join(parts)
                fp += 1
            elif prediction == ['TP']:
                if discard_existing_filters:
                    parts[6] = '.'
                    line = '\t'.join(parts)
                tp += 1
            else:
                unclassified += 1
            out.write(line)

            if validation_table:
                ver_line = df_truth.values[ln_cnt-1]
                ver_y = ver_line[7]
                ver_line = ver_line[0:7]
                for cnt in range(0, len(ver_line)):
                    if np.isnan(ver_line[cnt]):
                        ver_line[cnt] = 0.

                if ver_y != prediction[0]:
                    vprint(str(ln_cnt) + '. Truth: ' + ver_y + ' Test: ' + prediction[0], verbose)
                    vprint(ver_line, verbose)
                    vprint(params_line[0], verbose)
                    vprint('--------------', verbose)

vprint('TP=' + str(tp), verbose)
vprint('FP=' + str(fp), verbose)
vprint('unclassified=' + str(unclassified), verbose)
vprint('Not present: ' + str(not_pres), verbose)

# Verify
if validation_table:
    y_truth = df_truth.values[:, 7]
    y_predictions = np.array(y_predictions)

    report = classification_report(y_truth, y_predictions, digits = 4)
    vprint(report, verbose)
    all_tp = ['TP' for cnt in range(0, len(y_truth))]
    report_all_tp = classification_report(y_truth, all_tp, digits = 4)
    vprint(report_all_tp, verbose)


# Copyright 2018 Seven Bridges Genomics Inc. All rights reserved.

# Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:
# 1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
# 3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND
# FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.