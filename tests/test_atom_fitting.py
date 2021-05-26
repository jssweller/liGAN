import sys, os, pytest
from numpy import isclose, allclose, inf
import torch

sys.path.insert(0, '.')
from molgrid import GridMaker, Coords2Grid
from liGAN import molecules as mols
from liGAN.atom_types import Atom, AtomTyper
from liGAN.atom_grids import AtomGrid, size_to_dimension
from liGAN.atom_structs import AtomStruct
from liGAN.atom_fitting import AtomFitter
from liGAN.metrics import compute_struct_rmsd


test_sdf_files = [
    'data/O_2_0_0.sdf',
    'data/N_2_0_0.sdf',
    'data/C_2_0_0.sdf',
    'data/benzene.sdf',
    'data/neopentane.sdf',
    'data/sulfone.sdf',
    'data/ATP.sdf',
]


def write_pymol(
    visited_structs, grid, in_struct,
    fit_grid=None,
    kern_grid=None,
    conv_grid=None
):
    mol_name = in_struct.info['name']
    pymol_file = 'tests/TEST_' + mol_name + '_fit.pymol'
    with open(pymol_file, 'w') as f:

        if visited_structs:
            f.write('load {}\n'.format(
                write_structs(
                    visited_structs, in_struct.info['src_mol'], mol_name
                )
            ))

        f.write('load_group {}, {}\n'.format(
            *write_grid(grid, mol_name, 'lig')
        ))

        if fit_grid is not None:
            f.write('load_group {}, {}\n'.format(
                *write_grid(fit_grid, mol_name, 'lig_fit')
            ))

        if kern_grid is not None:
            f.write('load_group {}, {}\n'.format(
                *write_grid(kern_grid, mol_name, 'lig_kern')
            ))

        if conv_grid is not None:
            f.write('load_group {}, {}\n'.format(
                *write_grid(conv_grid, mol_name, 'lig_conv')
            ))

        f.write('show_as nb_spheres\n')
        f.write('show sticks\n')
        f.write('util.cbam\n')
        f.write('set_atom_level 0.5, job_name=TEST')


def write_grid(grid, mol_name, grid_type):
    dx_prefix = 'tests/TEST_{}_{}_0'.format(mol_name, grid_type)
    dx_files = grid.to_dx(dx_prefix)
    return dx_prefix + '*.dx', dx_prefix


def make_grid(grid, elem_values):
    nc, sz = grid.n_prop_channels, elem_values.shape[1]
    prop_values = torch.zeros(nc, sz, sz, sz, device=elem_values.device)
    return grid.new_like(
        values=torch.cat([elem_values, prop_values], dim=0)
    )


def write_structs(visited_structs, in_mol, mol_name):
    visited_mols = [m.to_ob_mol()[0] for m in visited_structs]
    write_mols = visited_mols + [in_mol]
    mol_file = 'tests/TEST_{}_fit.sdf'.format(mol_name)
    mols.write_ob_mols_to_sdf_file(mol_file, write_mols)
    return mol_file


@pytest.fixture(params=range(10))
def idx(request):
    return request.param


class TestAtomFitter(object):

    @pytest.fixture(params=[
        '-c', '-v',
        #'oad-c', 'oadc-c', 'on-c', 'oh-c',
        #'oad-v', 'oadc-v', 'on-v', 'oh-v',
    ])
    def typer(self, request):
        prop_funcs, radius_func = request.param.split('-')
        typer = AtomTyper.get_typer(
            prop_funcs=prop_funcs,
            radius_func=radius_func,
        )
        typer.name = request.param
        return typer

    @pytest.fixture
    def gridder(self):
        resolution = 0.5
        return Coords2Grid(GridMaker(
            resolution=resolution,
            dimension=size_to_dimension(
                size=24,
                resolution=resolution
            ),
        ))

    @pytest.fixture
    def fitter(self):
        return AtomFitter(
            multi_atom=False,
            n_atoms_detect=1,
            apply_conv=False,
            threshold=0.1,
            peak_value=1.5,
            min_dist=0,
            interm_gd_iters=10,
            final_gd_iters=100,
            gd_kwargs=dict(lr=0.1),
            verbose=True,
            device='cuda'
        )

    @pytest.fixture(params=test_sdf_files)
    def mol(self, request):
        sdf_file = request.param
        mol, atoms = mols.read_ob_mols_from_file(sdf_file, 'sdf')
        mol.AddHydrogens() # this is needed to determine donor/acceptor
        mol.name = os.path.splitext(os.path.basename(sdf_file))[0]
        return mol

    @pytest.fixture
    def struct(self, mol, typer):
        return typer.make_struct(
            mol,
            name='{}_{}'.format(mol.name, typer.name),
            dtype=torch.float32,
            device='cuda'
        )

    @pytest.fixture
    def grid(self, struct, gridder):
        gridder.center = tuple(float(v) for v in struct.center)
        return AtomGrid(
            values=gridder.forward(
                coords=struct.coords,
                types=struct.types,
                radii=struct.atomic_radii,
            ),
            center=struct.center,
            resolution=gridder.gmaker.get_resolution(),
            typer=struct.typer,
            src_struct=struct
        )

    def test_init(self, fitter):
        pass

    def test_gridder1(self, grid):
        assert grid.values.norm() > 0, 'empty grid'
        for type_vec in grid.info['src_struct'].types:
            assert (grid.values[type_vec>0].sum(dim=(1,2,3)) > 0).all(), \
                'empty grid channel'

    def test_gridder2(self, struct, grid, fitter):
        # NOTE if dimensions are slightly different, even if it's
        #   less than resolution so that the grid shapes are same,
        #   the grid values will be different due to centering
        fitter.grid_maker.set_dimension(grid.dimension)
        fitter.grid_maker.set_resolution(grid.resolution)
        fitter.c2grid.center = tuple(float(v) for v in struct.center)
        values = fitter.c2grid.forward(
            coords=struct.coords,
            types=struct.types,
            radii=struct.atomic_radii
        )
        assert values.shape == grid.values.shape, 'different grid shapes'
        assert (values == grid.values).all(), 'different grid values'

    def test_init_kernel(self, typer, fitter):
        assert fitter.kernel is None, 'kernel already initialized'
        kernel = fitter.init_kernel(resolution=0.5, typer=typer)
        assert kernel.shape[0] == typer.n_elem_types, 'wrong num channels'
        assert kernel.shape[1] % 2 == 1, 'kernel size is even'
        assert kernel.norm() > 0, 'empty kernel'
        assert ((kernel**2).sum(dim=(1,2,3)) > 0).all(), 'empty kernel channel'

        m = kernel.shape[1]//2 # midpoint index
        assert (kernel[:,m-1,m,m] == kernel[:,m+1,m,m]).all(), 'kernel not symmetric'
        assert (kernel[:,m,m-1,m] == kernel[:,m,m+1,m]).all(), 'kernel not symmetric'
        assert (kernel[:,m,m,m-1] == kernel[:,m,m,m+1]).all(), 'kernel not symmetric'
        assert (kernel[:,m,m,m] == 1.0).all(), 'kernel not centered'

    def test_convolve(self, fitter, grid):
        grid_values = grid.elem_values
        conv_values = fitter.convolve(
            grid_values, grid.resolution, grid.typer
        )
        #kern_grid = make_grid(grid, fitter.kernel)
        #conv_grid = make_grid(grid, conv_values)
        #mol = grid.info['src_struct'].info['src_mol']
        #write_pymol([], grid, mol, kern_grid=kern_grid, conv_grid=conv_grid)

        dims = (1,2,3) # compute channel norms
        grid_norm2 = (grid_values**2).sum(dim=dims)**0.5
        conv_norm2 = (conv_values**2).sum(dim=dims)**0.5
        kern_norm2 = (fitter.kernel**2).sum(dim=dims)**0.5
        assert (conv_norm2 >= grid_norm2).all(), 'channel norm decreased'
        assert (conv_values > 0.5).any(), 'failed to detect atoms'

    def test_apply_peak_value(self, fitter, grid):
        peak_values = fitter.apply_peak_value(grid.elem_values)
        assert (peak_values <= fitter.peak_value).all(), 'values above peak'

    def test_sort_grid_points(self, fitter, grid):
        values, idx_xyz, idx_c = fitter.sort_grid_points(grid.elem_values)
        idx_x, idx_y, idx_z = idx_xyz[:,0], idx_xyz[:,1], idx_xyz[:,2]
        assert (values[:-1] >= values[1:]).all(), 'values not sorted'
        assert (grid.elem_values[idx_c, idx_x, idx_y, idx_z] == values).all(), \
            'values not unsorted'

    def test_apply_threshold(self, fitter, grid):
        values, idx_xyz, idx_c = fitter.sort_grid_points(grid.elem_values)
        values, idx_xyz, idx_c = fitter.apply_threshold(values, idx_xyz, idx_c)
        assert (values > fitter.threshold).all(), 'values below threshold'

    def test_suppress_non_max(self, fitter, grid):
        values, idx_xyz, idx_c = fitter.sort_grid_points(grid.elem_values)
        values, idx_xyz, idx_c = fitter.apply_threshold(values, idx_xyz, idx_c)
        coords = grid.get_coords(idx_xyz)
        coords_mat, idx_xyz_mat, idx_c_mat = fitter.suppress_non_max(
            values, coords, idx_xyz, idx_c, grid, matrix=True
        )
        coords_for, idx_xyz_for, idx_c_for = fitter.suppress_non_max(
            values, coords, idx_xyz, idx_c, grid, matrix=False
        )
        assert len(coords_mat) == len(idx_c_mat)
        assert len(coords_for) == len(idx_c_for)
        assert len(coords_mat) == len(coords_for)
        assert len(coords_mat) <= len(coords)
        assert coords_mat.shape[1] == 3
        assert coords_for.shape[1] == 3
        assert (coords_mat == coords_for).all()
        assert (idx_c_mat == idx_c_for).all()

    def test_detect_atoms(self, fitter, grid):
        fitter.n_atoms_detect = None
        coords, types = fitter.detect_atoms(grid)
        struct = AtomStruct(
            coords, types, grid.typer,
            src_mol=grid.info['src_struct'].info['src_mol']
        )
        #write_pymol([struct], grid, struct)
        if fitter.n_atoms_detect is not None:
            assert coords.shape == (fitter.n_atoms_detect, 3)
            assert types.shape == (fitter.n_atoms_detect, grid.n_channels)
        assert coords.dtype == types.dtype == grid.dtype
        assert coords.device == types.device == grid.device

    def test_fit_struct(self, fitter, grid):
        struct = grid.info['src_struct']
        fit_struct, fit_grid, visited_structs = fitter.fit_struct(grid)
        write_pymol(visited_structs, grid, struct, fit_grid=fit_grid)

        assert fit_struct == visited_structs[-1], 'final struct is not last visited'
        final_loss = fit_struct.info['L2_loss']
        for i, struct_i in enumerate(visited_structs):
            loss_i = struct_i.info['L2_loss']
            assert final_loss <= loss_i, \
                'final struct is not best ({:.2f} > {:.2f})'.format(final_loss, loss_i)

        rmsd = compute_struct_rmsd(struct, fit_struct)
        assert rmsd < 0.5, 'RMSD too high ({:.2f})'.format(rmsd)
