function verify_dyno_matlab(exampleFolder, outputFolder)
% Read model parameters and produce independent MATLAB packet references.
% Neither host nor target model is simulated; no serial port is opened.
if nargin < 1 || ~isfolder(exampleFolder)
    error('Provide the DualMotorDynoExample folder containing the official models.');
end
if nargin < 2
    outputFolder = fullfile(fileparts(fileparts(mfilename('fullpath'))), 'tests', 'fixtures');
end
example = exampleFolder;
run(fullfile(example, 'mcb_pmsm_foc_dyno_f28379d_data.m'));
host = 'mcb_pmsm_foc_host_model_dyno';
load_system(fullfile(example, [host '.slx']));

fixtures.motor1 = bases(PU_System_motor1);
fixtures.motor2 = bases(PU_System_motor2);
fixtures.source = [host '.slx'];
fixtures.rnd_meth = get_param([host '/Serial Communication/Data_Conditioning_Tx/Data_Type_Fixed_Point/Data Type Conversion'], 'RndMeth');
fixtures.serial = struct('baudrate', get_param([host '/Host Serial Setup'], 'BaudRate'), ...
    'byte_order', get_param([host '/Host Serial Setup'], 'ByteOrder'));
inputCases = [1200 1 90 0 1; -1200 -1 270 15 0; 0.001 0.0001 0.001 7 1; 2000 2 180 12 1];
cases = cell(1, size(inputCases, 1));
for index = 1:size(inputCases, 1)
    values = inputCases(index, :);
    pu = single(values(1:3)) .* single([1/PU_System_motor1.N_base, 1/PU_System_motor2.I_base, 1/360]);
    control = bitor(bitshift(uint32(values(4)), 4), uint32(values(5)));
    floating = single([pu(1), single(control), pu(2), pu(3)]);
    % The supplied block uses Simplest: floating-to-fixed rounds toward zero.
    fixed = fi(pu, 1, 32, 28, 'RoundingMethod', 'Zero', 'OverflowAction', 'Wrap');
    raw = typecast(storedInteger(fixed), 'uint32');
    fixedPacket = uint32([raw(1), control, raw(2), raw(3)]);
    cases{index} = struct('rpm', values(1), 'imag_ref', values(2), 'imag_pos', values(3), ...
        'selection', values(4), 'enabled', logical(values(5)), ...
        'single_hex', packetHex(floating), 'q28_hex', packetHex(fixedPacket));
end
fixtures.cases = [cases{:}];

switchBlock = [host '/Serial Communication/Convert_to_SI_Units/Multiport' newline 'Switch'];
ports = get_param(switchBlock, 'PortHandles');
groups = cell(1, 16);
for selection = 0:15
    wire = get_param(ports.Inport(selection + 2), 'Line');
    source = get_param(wire, 'SrcBlockHandle');
    expression = get_param(source, 'Value');
    tokens = regexp(expression, 'PU_System_motor([12])\.([NIVPT])_base', 'tokens');
    scales = [1, 1];
    for index = 1:numel(tokens)
        if strcmp(tokens{index}{1}, '1')
            system = PU_System_motor1;
        else
            system = PU_System_motor2;
        end
        scales(index) = system.([tokens{index}{2} '_base']);
    end
    groups{selection + 1} = struct('selection', selection, 'expression', expression, 'scales', scales);
end
fixtures.groups = [groups{:}];
if ~isfolder(outputFolder)
    mkdir(outputFolder);
end
destination = fullfile(outputFolder, 'dyno_matlab_reference.json');
fid = fopen(destination, 'w', 'n', 'UTF-8');
if fid < 0
    error('Cannot create reference file: %s', destination);
end
cleanup = onCleanup(@() fclose(fid));
fprintf(fid, '%s\n', jsonencode(fixtures, 'PrettyPrint', true));
fprintf('Dyno MATLAB reference: %s\n', destination);
end

function result = bases(system)
result = struct('n_base', system.N_base, 'i_base', system.I_base, ...
    'v_base', system.V_base, 'p_base', system.P_base, 't_base', system.T_base);
end

function text = packetHex(values)
bytes = typecast(values, 'uint8');
text = strtrim(sprintf('%02X ', bytes));
end
