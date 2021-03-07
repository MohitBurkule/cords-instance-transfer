import argparse
import time
import datetime
import copy
import numpy as np
import os
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data.sampler import SubsetRandomSampler
from cords.utils.models import *
from cords.utils.custom_dataset import load_dataset_custom
from torch.utils.data import Subset
from math import floor
from cords.utils.config_utils import load_config_data
import os.path as osp


"""
Argument Parsing
"""
parser = argparse.ArgumentParser(description='Training arguments')
parser.add_argument('--config_file', type=str, default="configs/default_config.yaml",
                    help='Config File Location')
args = parser.parse_args()

configdata = load_config_data(args.config_file)

if configdata['setting'] == 'supervisedlearning':
    from cords.selectionstrategies.supervisedlearning import *
elif configdata['setting'] == 'general':
    from cords.selectionstrategies.general import *

"""
Loss Evaluation
"""
def model_eval_loss(data_loader, model, criterion):
    total_loss = 0
    with torch.no_grad():
        for batch_idx, (inputs, targets) in enumerate(data_loader):
            inputs, targets = inputs.to(configdata['train_args']['device']), targets.to(configdata['train_args']['device'], non_blocking=True)
            outputs = model(inputs)
            loss = criterion(outputs, targets)
            total_loss += loss.item()
    return total_loss

"""
#Model Creation
"""
def create_model():
    if configdata['model']['architecture'] == 'ResNet18':
        model = ResNet18(configdata['model']['numclasses'])
    elif configdata['model']['architecture'] == 'MnistNet':
        model = MnistNet()
    elif configdata['model']['architecture'] == 'ResNet164':
        model = ResNet164(configdata['model']['numclasses'])
    model = model.to(configdata['train_args']['device'])
    return model


"""#Loss Type, Optimizer and Learning Rate Scheduler"""
def loss_function():
    if configdata['loss']['type'] == "CrossEntropyLoss":
        criterion = nn.CrossEntropyLoss()
        criterion_nored = nn.CrossEntropyLoss(reduction='none')
    return criterion, criterion_nored

def optimizer_with_scheduler(model):
    if configdata['optimizer']['type'] == 'sgd':
        optimizer = optim.SGD(model.parameters(), lr=configdata['optimizer']['lr'],
                              momentum=configdata['optimizer']['momentum'], weight_decay=configdata['optimizer']['weight_decay'])

    if configdata['scheduler']['type'] == 'cosine_annealing':
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=configdata['scheduler']['T_max'])
    return optimizer, scheduler

def generate_cumulative_timing(mod_timing):
    tmp = 0
    mod_cum_timing = np.zeros(len(mod_timing))
    for i in range(len(mod_timing)):
        tmp += mod_timing[i]
        mod_cum_timing[i] = tmp
    return mod_cum_timing / 3600

from scipy.signal import lfilter
def filter(y):
    n = 1  # the larger n is, the smoother curve will be
    b = [1.0 / n] * n
    a = 1
    yy = lfilter(b, a, y)
    return yy


"""
#General Training Loop with Data Selection Strategies
"""




# Loading the Dataset
trainset, validset, testset, num_cls = load_dataset_custom(configdata['dataset']['datadir'], configdata['dataset']['name'], configdata['dataset']['feature'])
N = len(trainset)
trn_batch_size = 20
val_batch_size = 1000
tst_batch_size = 1000

# Creating the Data Loaders
trainloader = torch.utils.data.DataLoader(trainset, batch_size=trn_batch_size,
                                          shuffle=False, pin_memory=True)

valloader = torch.utils.data.DataLoader(validset, batch_size=val_batch_size,
                                        shuffle=False, pin_memory=True)

testloader = torch.utils.data.DataLoader(testset, batch_size=tst_batch_size,
                                         shuffle=False, pin_memory=True)

# Budget for subset selection
bud = int(configdata['dss_strategy']['fraction'] * N)
print("Budget, fraction and N:", bud, configdata['dss_strategy']['fraction'], N)

# Subset Selection and creating the subset data loader
start_idxs = np.random.choice(N, size=bud, replace=False)
idxs = start_idxs
data_sub = Subset(trainset, idxs)
subset_trnloader = torch.utils.data.DataLoader(data_sub,
                                               batch_size=configdata['dataloader']['batch_size'],
                                               shuffle=configdata['dataloader']['shuffle'],
                                               pin_memory=configdata['dataloader']['pin_memory'])

# Variables to store accuracies
gammas = torch.ones(len(idxs)).to(configdata['train_args']['device'])
substrn_losses = list() #np.zeros(configdata['train_args']['num_epochs'])
trn_losses = list()
val_losses = list() #np.zeros(configdata['train_args']['num_epochs'])
tst_losses = list()
subtrn_losses = list()
timing = np.zeros(configdata['train_args']['num_epochs'])
trn_acc = list()
val_acc = list() #np.zeros(configdata['train_args']['num_epochs'])
tst_acc = list() #np.zeros(configdata['train_args']['num_epochs'])
subtrn_acc = list() #np.zeros(configdata['train_args']['num_epochs'])


# Results logging file
print_every = configdata['train_args']['print_every']
results_dir = osp.abspath(osp.expanduser(configdata['train_args']['results_dir']))
all_logs_dir = os.path.join(results_dir,configdata['dss_strategy']['type'], configdata['dataset']['name'], str(
   configdata['dss_strategy']['fraction']), str(configdata['dss_strategy']['select_every']))

os.makedirs(all_logs_dir, exist_ok=True)
path_logfile = os.path.join(all_logs_dir, configdata['dataset']['name'] + '.txt')
logfile = open(path_logfile, 'w')

# Model Creation
model = create_model()
model1 = create_model()

# Loss Functions
criterion, criterion_nored = loss_function()

# Getting the optimizer and scheduler
optimizer, scheduler = optimizer_with_scheduler(model)

if configdata['dss_strategy']['type'] == 'GradMatch':
    # OMPGradMatch Selection strategy
    setf_model = OMPGradMatchStrategy(trainloader, valloader, model1, criterion,
                                      configdata['optimizer']['lr'], configdata['train_args']['device'], num_cls, True, 'PerClassPerGradient',
                                      False, lam=0.5, eps=1e-100)
elif configdata['dss_strategy']['type'] == 'GradMatchPB':
    setf_model = OMPGradMatchStrategy(trainloader, valloader, model1, criterion,
                                      configdata['optimizer']['lr'], configdata['train_args']['device'], num_cls, True, 'PerBatch',
                                      False, lam=0, eps=1e-100)
elif configdata['dss_strategy']['type'] == 'GLISTER':
    # GLISTER Selection strategy
    setf_model = GLISTERStrategy(trainloader, valloader, model1, criterion_nored,
                                 configdata['optimizer']['lr'], configdata['train_args']['device'], num_cls, False, 'Stochastic', r=int(bud))

elif configdata['dss_strategy']['type'] == 'CRAIG':
    # CRAIG Selection strategy
    setf_model = CRAIGStrategy(trainloader, valloader, model1, criterion,
                               configdata['train_args']['device'], num_cls, False, False, 'PerClass')

elif configdata['dss_strategy']['type'] == 'CRAIGPB':
    # CRAIG Selection strategy
    setf_model = CRAIGStrategy(trainloader, valloader, model1, criterion,
                               configdata['train_args']['device'], num_cls, False, False, 'PerBatch')

elif configdata['dss_strategy']['type'] == 'CRAIG-Warm':
    # CRAIG Selection strategy
    setf_model = CRAIGStrategy(trainloader, valloader, model1, criterion,
                               configdata['train_args']['device'], num_cls, False, False, 'PerClass')
    # Random-Online Selection strategy
    #rand_setf_model = RandomStrategy(trainloader, online=True)
    if configdata['dss_strategy'].has_key('kappa'):
        kappa_epochs = int(configdata['dss_strategy']['kappa'] * configdata['train_args']['num_epochs'])
        full_epochs = floor(kappa_epochs / int(configdata['dss_strategy']['fraction'] * 100))
    else:
        raise KeyError("Specify a kappa value in the config file")

elif configdata['dss_strategy']['type'] == 'CRAIGPB-Warm':
    # CRAIG Selection strategy
    setf_model = CRAIGStrategy(trainloader, valloader, model1, criterion,
                               configdata['train_args']['device'], num_cls, False, False, 'PerBatch')
    # Random-Online Selection strategy
    #rand_setf_model = RandomStrategy(trainloader, online=True)
    if configdata['dss_strategy'].has_key('kappa'):
        kappa_epochs = int(configdata['dss_strategy']['kappa'] * configdata['train_args']['num_epochs'])
        full_epochs = floor(kappa_epochs / int(configdata['dss_strategy']['fraction'] * 100))
    else:
        raise KeyError("Specify a kappa value in the config file")

elif configdata['dss_strategy']['type'] == 'Random':
    # Random Selection strategy
    setf_model = RandomStrategy(trainloader, online=False)

elif configdata['dss_strategy']['type'] == 'Random-Online':
    # Random-Online Selection strategy
    setf_model = RandomStrategy(trainloader, online=True)

elif configdata['dss_strategy']['type'] == 'GLISTER-Warm':
    # GLISTER Selection strategy
    setf_model = GLISTERStrategy(trainloader, valloader, model1, criterion,
                                 configdata['optimizer']['lr'], configdata['train_args']['device'], num_cls, False, 'Stochastic', r=int(bud))
    # Random-Online Selection strategy
    #rand_setf_model = RandomStrategy(trainloader, online=True)
    if configdata['dss_strategy'].has_key('kappa'):
        kappa_epochs = int(configdata['dss_strategy']['kappa'] * configdata['train_args']['num_epochs'])
        full_epochs = floor(kappa_epochs / int(configdata['dss_strategy']['fraction'] * 100))
    else:
        raise KeyError("Specify a kappa value in the config file")

elif configdata['dss_strategy']['type'] == 'GradMatch-Warm':
    # OMPGradMatch Selection strategy
    setf_model = OMPGradMatchStrategy(trainloader, valloader, model1, criterion,
                                      configdata['optimizer']['lr'], configdata['train_args']['device'], num_cls, True, 'PerClassPerGradient',
                                      False, lam=0.5, eps=1e-100)
    # Random-Online Selection strategy
    #rand_setf_model = RandomStrategy(trainloader, online=True)
    if configdata['dss_strategy'].has_key('kappa'):
        kappa_epochs = int(configdata['dss_strategy']['kappa'] * configdata['train_args']['num_epochs'])
        full_epochs = floor(kappa_epochs / int(configdata['dss_strategy']['fraction'] * 100))
    else:
        raise KeyError("Specify a kappa value in the config file")

elif configdata['dss_strategy']['type'] == 'GradMatchPB-Warm':
    # OMPGradMatch Selection strategy
    setf_model = OMPGradMatchStrategy(trainloader, valloader, model1, criterion,
                                      configdata['optimizer']['lr'], configdata['train_args']['device'], num_cls, True, 'PerBatch',
                                      False, lam=0, eps=1e-100)
    # Random-Online Selection strategy
    #rand_setf_model = RandomStrategy(trainloader, online=True)
    if configdata['dss_strategy'].has_key('kappa'):
        kappa_epochs = int(configdata['dss_strategy']['kappa'] * configdata['train_args']['num_epochs'])
        full_epochs = floor(kappa_epochs / int(configdata['dss_strategy']['fraction'] * 100))
    else:
        raise KeyError("Specify a kappa value in the config file")

print("=======================================", file=logfile)

for i in range(configdata['train_args']['num_epochs']):
    subtrn_loss = 0
    subtrn_correct = 0
    subtrn_total = 0
    subset_selection_time = 0

    if configdata['dss_strategy']['type'] in ['Random-Online']:
        start_time = time.time()
        subset_idxs, gammas = setf_model.select(int(bud))
        idxs = subset_idxs
        subset_selection_time += (time.time() - start_time)
        gammas = gammas.to(configdata['train_args']['device'])

    elif configdata['dss_strategy']['type'] in ['Random']:
        pass

    elif (configdata['dss_strategy']['type'] in ['GLISTER', 'GradMatch', 'GradMatchPB', 'CRAIG', 'CRAIGPB']) and (
            ((i + 1) % configdata['dss_strategy']['select_every']) == 0):
        start_time = time.time()
        cached_state_dict = copy.deepcopy(model.state_dict())
        clone_dict = copy.deepcopy(model.state_dict())
        if configdata['dss_strategy']['type'] in ['CRAIG', 'CRAIGPB']:
            subset_idxs, gammas = setf_model.select(int(bud), clone_dict, 'lazy')
        else:
            subset_idxs, gammas = setf_model.select(int(bud), clone_dict)
        model.load_state_dict(cached_state_dict)
        idxs = subset_idxs
        if configdata['dss_strategy']['type'] in ['GradMatch', 'GradMatchPB', 'CRAIG', 'CRAIGPB']:
            gammas = torch.from_numpy(np.array(gammas)).to(configdata['train_args']['device']).to(torch.float32)
        subset_selection_time += (time.time() - start_time)

    elif (configdata['dss_strategy']['type'] in ['GLISTER-Warm', 'GradMatch-Warm', 'GradMatchPB-Warm', 'CRAIG-Warm',
                       'CRAIGPB-Warm']):
        start_time = time.time()

        if ((i % configdata['dss_strategy']['select_every'] == 0) and (i >= kappa_epochs)):
            cached_state_dict = copy.deepcopy(model.state_dict())
            clone_dict = copy.deepcopy(model.state_dict())
            if configdata['dss_strategy']['type'] in ['CRAIG-Warm', 'CRAIGPB-Warm']:
                subset_idxs, gammas = setf_model.select(int(bud), clone_dict, 'lazy')
            else:
                subset_idxs, gammas = setf_model.select(int(bud), clone_dict)
            model.load_state_dict(cached_state_dict)
            idxs = subset_idxs
            if configdata['dss_strategy']['type'] in ['GradMatch-Warm', 'GradMatchPB-Warm', 'CRAIG-Warm', 'CRAIGPB-Warm']:
                gammas = torch.from_numpy(np.array(gammas)).to(configdata['train_args']['device']).to(torch.float32)
        subset_selection_time += (time.time() - start_time)

    print("selEpoch: %d, Selection Ended at:" % (i), str(datetime.datetime.now()))
    data_sub = Subset(trainset, idxs)
    subset_trnloader = torch.utils.data.DataLoader(data_sub, batch_size=trn_batch_size, shuffle=False,
                                                   pin_memory=True)

    model.train()
    batch_wise_indices = list(subset_trnloader.batch_sampler)
    if configdata['dss_strategy']['type'] in ['CRAIG', 'CRAIGPB', 'GradMatch', 'GradMatchPB']:
        start_time = time.time()
        for batch_idx, (inputs, targets) in enumerate(subset_trnloader):
            inputs, targets = inputs.to(configdata['train_args']['device']), targets.to(configdata['train_args']['device'],
                                                                                           non_blocking=True)  # targets can have non_blocking=True.
            optimizer.zero_grad()
            outputs = model(inputs)
            losses = criterion_nored(outputs, targets)
            loss = torch.dot(losses, gammas[batch_wise_indices[batch_idx]]) / (gammas[batch_wise_indices[batch_idx]].sum())
            loss.backward()
            subtrn_loss += loss.item()
            optimizer.step()
            _, predicted = outputs.max(1)
            subtrn_total += targets.size(0)
            subtrn_correct += predicted.eq(targets).sum().item()
        train_time = time.time() - start_time

    elif configdata['dss_strategy']['type'] in ['CRAIGPB-Warm', 'CRAIG-Warm', 'GradMatch-Warm', 'GradMatchPB-Warm']:
        start_time = time.time()
        if i < full_epochs:
            for batch_idx, (inputs, targets) in enumerate(trainloader):
                inputs, targets = inputs.to(configdata['train_args']['device']), targets.to(configdata['train_args']['device'],
                                                                                               non_blocking=True)  # targets can have non_blocking=True.
                optimizer.zero_grad()
                outputs = model(inputs)
                loss = criterion(outputs, targets)
                loss.backward()
                subtrn_loss += loss.item()
                optimizer.step()
                _, predicted = outputs.max(1)
                subtrn_total += targets.size(0)
                subtrn_correct += predicted.eq(targets).sum().item()

        elif i >= kappa_epochs:
            for batch_idx, (inputs, targets) in enumerate(subset_trnloader):
                inputs, targets = inputs.to(configdata['train_args']['device']), targets.to(configdata['train_args']['device'],
                                                                                               non_blocking=True)  # targets can have non_blocking=True.
                optimizer.zero_grad()
                outputs = model(inputs)
                losses = criterion_nored(outputs, targets)
                loss = torch.dot(losses, gammas[batch_wise_indices[batch_idx]]) / (
                    gammas[batch_wise_indices[batch_idx]].sum())
                loss.backward()
                subtrn_loss += loss.item()
                optimizer.step()
                _, predicted = outputs.max(1)
                subtrn_total += targets.size(0)
                subtrn_correct += predicted.eq(targets).sum().item()
        train_time = time.time() - start_time

    elif configdata['dss_strategy']['type'] in ['GLISTER', 'Random', 'Random-Online']:
        start_time = time.time()
        for batch_idx, (inputs, targets) in enumerate(subset_trnloader):
            inputs, targets = inputs.to(configdata['train_args']['device']), targets.to(configdata['train_args']['device'],
                                                                                           non_blocking=True)  # targets can have non_blocking=True.
            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs, targets)
            loss.backward()
            subtrn_loss += loss.item()
            optimizer.step()
            _, predicted = outputs.max(1)
            subtrn_total += targets.size(0)
            subtrn_correct += predicted.eq(targets).sum().item()
        train_time = time.time() - start_time

    elif configdata['dss_strategy']['type'] in ['GLISTER-Warm']:
        start_time = time.time()
        if i < full_epochs:
            for batch_idx, (inputs, targets) in enumerate(trainloader):
                inputs, targets = inputs.to(configdata['train_args']['device']), targets.to(configdata['train_args']['device'],
                                                                                               non_blocking=True)  # targets can have non_blocking=True.
                optimizer.zero_grad()
                outputs = model(inputs)
                loss = criterion(outputs, targets)
                loss.backward()
                subtrn_loss += loss.item()
                optimizer.step()
                _, predicted = outputs.max(1)
                subtrn_total += targets.size(0)
                subtrn_correct += predicted.eq(targets).sum().item()
        elif i >= kappa_epochs:
            for batch_idx, (inputs, targets) in enumerate(subset_trnloader):
                inputs, targets = inputs.to(configdata['train_args']['device']), targets.to(configdata['train_args']['device'],
                                                                                               non_blocking=True)  # targets can have non_blocking=True.
                optimizer.zero_grad()
                outputs = model(inputs)
                loss = criterion(outputs, targets)
                loss.backward()
                subtrn_loss += loss.item()
                optimizer.step()
                _, predicted = outputs.max(1)
                subtrn_total += targets.size(0)
                subtrn_correct += predicted.eq(targets).sum().item()
        train_time = time.time() - start_time

    elif configdata['dss_strategy']['type'] in ['Full']:
        start_time = time.time()
        for batch_idx, (inputs, targets) in enumerate(trainloader):
            inputs, targets = inputs.to(configdata['train_args']['device']), targets.to(configdata['train_args']['device'],
                                                                                           non_blocking=True)  # targets can have non_blocking=True.
            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs, targets)
            loss.backward()
            subtrn_loss += loss.item()
            optimizer.step()
            _, predicted = outputs.max(1)
            subtrn_total += targets.size(0)
            subtrn_correct += predicted.eq(targets).sum().item()
        train_time = time.time() - start_time
    scheduler.step()
    timing[i] = train_time + subset_selection_time
    print_args = configdata['train_args']['print_args']
    # print("Epoch timing is: " + str(timing[i]))
    if (i % configdata['train_args']['print_every'] == 0):
        trn_loss = 0
        trn_correct = 0
        trn_total = 0
        val_loss = 0
        val_correct = 0
        val_total = 0
        tst_correct = 0
        tst_total = 0
        tst_loss = 0
        model.eval()

        if "trn_loss" in print_args:
            with torch.no_grad():
                for batch_idx, (inputs, targets) in enumerate(trainloader):
                    # print(batch_idx)
                    inputs, targets = inputs.to(configdata['train_args']['device']), targets.to(configdata['train_args']['device'], non_blocking=True)
                    outputs = model(inputs)
                    loss = criterion(outputs, targets)
                    trn_loss += loss.item()
                    trn_losses.append(trn_loss)
                    if "trn_acc" in print_args:
                        _, predicted = outputs.max(1)
                        trn_total += targets.size(0)
                        trn_correct += predicted.eq(targets).sum().item()
                        trn_acc.append(trn_correct / trn_total)

        if "val_loss" in print_args:
            with torch.no_grad():
                for batch_idx, (inputs, targets) in enumerate(valloader):
                    # print(batch_idx)
                    inputs, targets = inputs.to(configdata['train_args']['device']), targets.to(configdata['train_args']['device'], non_blocking=True)
                    outputs = model(inputs)
                    loss = criterion(outputs, targets)
                    val_loss += loss.item()
                    val_losses.append(val_loss)
                    if "val_acc" in print_args:
                        _, predicted = outputs.max(1)
                        val_total += targets.size(0)
                        val_correct += predicted.eq(targets).sum().item()
                        val_acc.append(val_correct / val_total)

        if "tst_loss" in print_args:
            for batch_idx, (inputs, targets) in enumerate(testloader):
                # print(batch_idx)
                inputs, targets = inputs.to(configdata['train_args']['device']), targets.to(configdata['train_args']['device'], non_blocking=True)
                outputs = model(inputs)
                loss = criterion(outputs, targets)
                tst_loss += loss.item()
                tst_losses.append(tst_loss)
                if "tst_acc" in print_args:
                    _, predicted = outputs.max(1)
                    tst_total += targets.size(0)
                    tst_correct += predicted.eq(targets).sum().item()
                    tst_acc.append(tst_correct/tst_total)

        if "subtrn_acc" in print_args:
            subtrn_acc.append(subtrn_correct / subtrn_total)

        if "subtrn_losses" in print_args:
            subtrn_losses.append(subtrn_loss)

        print_str = "Epoch: " + str(i+1)

        for arg in print_args:

            if arg == "val_loss":
                print_str += " , " + "Validation Loss: " + val_losses[-1]

            if arg == "val_acc":
                print_str += " , " + "Validation Accuracy: " + val_acc[-1]

            if arg == "tst_loss":
                print_str += " , " + "Test Loss: " + tst_losses[-1]

            if arg == "tst_acc":
                print_str += " , " + "Test Accuracy: " + tst_acc[-1]

            if arg == "trn_loss":
                print_str += " , " + "Training Loss: " + trn_losses[-1]

            if arg == "trn_acc":
                print_str += " , " + "Training Accuracy: " + trn_acc[-1]

            if arg == "subtrn_loss":
                print_str += " , " + "Subset Loss: " + subtrn_losses[-1]

            if arg == "subtrn_acc":
                print_str += " , " + "Subset Accuracy: " + subtrn_acc[-1]

            if arg == "time":
                print_str += " , " + "Timing: " + timing[i]

        print(print_str)

    print(configdata['dss_strategy']['type'] + " Selection Run---------------------------------")
    print("Final SubsetTrn:", subtrn_loss)
    print("Validation Loss and Accuracy:", val_loss, val_acc.max())
    print("Test Data Loss and Accuracy:", tst_loss, tst_acc.max())
    print('-----------------------------------')

    # Results logging into the file
    print(configdata['dss_strategy']['type'], file=logfile)
    print('---------------------------------------------------------------------', file=logfile)
    val_str = "Validation Accuracy, "
    tst_str = "Test Accuracy, "
    time_str = "Time, "

    for time in timing:
        time_str = time_str + " , " + str(time)

    for val in val_acc:
        val_str = val_str + " , " + str(val)

    for tst in tst_acc:
        tst_str = tst_str + " , " + str(tst)

    print(timing, file=logfile)
    print(val_str, file=logfile)
    print(tst_str, file=logfile)

    omp_timing = np.array(timing)
    omp_cum_timing = list(generate_cumulative_timing(omp_timing))
    #omp_tst_acc = list(filter(tst_acc))
    print("Total time taken by " + configdata['dss_strategy']['type'] + " = " + str(omp_cum_timing[-1]))
    logfile.close()


